# Copyright (c) 2016-present, Facebook, Inc.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import json
import logging
import sys
import traceback
from logging import Logger
from pathlib import Path
from typing import List, Optional

from typing_extensions import Final

from ...client.commands import ExitCode
from . import UserError
from .ast import UnstableAST
from .codemods import MissingGlobalAnnotations, MissingOverrideReturnAnnotations
from .commands.command import (
    Command,
    ErrorSuppressingCommand,
    ProjectErrorSuppressingCommand,
)
from .commands.consolidate_nested_configurations import ConsolidateNestedConfigurations
from .commands.expand_target_coverage import ExpandTargetCoverage
from .commands.fixme import Fixme
from .commands.strict_default import StrictDefault
from .commands.targets_to_configuration import TargetsToConfiguration
from .configuration import Configuration
from .errors import errors_from_targets
from .filesystem import LocalMode, Target, add_local_mode, find_targets, path_exists
from .repository import Repository


LOG: Logger = logging.getLogger(__name__)


class GlobalVersionUpdate(Command):
    def __init__(self, arguments: argparse.Namespace, repository: Repository) -> None:
        super().__init__(arguments, repository)
        self._hash: str = self._arguments.hash
        self._paths: List[Path] = self._arguments.paths
        self._submit: bool = self._arguments.submit

    def run(self) -> None:
        global_configuration = Configuration.find_project_configuration()
        with open(global_configuration, "r") as global_configuration_file:
            configuration = json.load(global_configuration_file)
            if "version" not in configuration:
                LOG.error(
                    "Global configuration at %s has no version field.",
                    global_configuration,
                )
                return

            old_version = configuration["version"]

        # Rewrite.
        with open(global_configuration, "w") as global_configuration_file:
            configuration["version"] = self._hash

            # This will sort the keys in the configuration - we won't be clobbering
            # comments since Python's JSON parser disallows comments either way.
            json.dump(
                configuration, global_configuration_file, sort_keys=True, indent=2
            )
            global_configuration_file.write("\n")

        paths = self._paths
        configuration_paths = (
            [path / ".pyre_configuration.local" for path in paths]
            if paths
            else [
                configuration.get_path()
                for configuration in Configuration.gather_local_configurations()
                if configuration.is_local
            ]
        )
        for configuration_path in configuration_paths:
            if "mock_repository" in str(configuration_path):
                # Skip local configurations we have for testing.
                continue
            with open(configuration_path) as configuration_file:
                contents = json.load(configuration_file)
                if "version" in contents:
                    LOG.info(
                        "Skipping %s as it already has a custom version field.",
                        configuration_path,
                    )
                    continue
                if contents.get("differential"):
                    LOG.info(
                        "Skipping differential configuration at `%s`",
                        configuration_path,
                    )
                    continue
                contents["version"] = old_version

            with open(configuration_path, "w") as configuration_file:
                json.dump(contents, configuration_file, sort_keys=True, indent=2)
                configuration_file.write("\n")

        self._repository.submit_changes(
            commit=True,
            submit=self._submit,
            title="Update pyre global configuration version",
            summary=f"Automatic upgrade to hash `{self._hash}`",
            ignore_failures=True,
        )


class FixmeSingle(ProjectErrorSuppressingCommand):
    def __init__(self, arguments: argparse.Namespace, repository: Repository) -> None:
        super().__init__(arguments, repository)
        self._path: Path = Path(arguments.path)

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        ProjectErrorSuppressingCommand.add_arguments(parser)

    def run(self) -> None:
        project_configuration = Configuration.find_project_configuration()
        configuration_path = self._path / ".pyre_configuration.local"
        with open(configuration_path) as configuration_file:
            configuration = Configuration(
                configuration_path, json.load(configuration_file)
            )
            self._suppress_errors_in_project(
                configuration, project_configuration.parent
            )


class FixmeAll(ProjectErrorSuppressingCommand):
    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        ProjectErrorSuppressingCommand.add_arguments(parser)

    def run(self) -> None:
        project_configuration = Configuration.find_project_configuration()
        configurations = Configuration.gather_local_configurations()
        for configuration in configurations:
            self._suppress_errors_in_project(
                configuration, project_configuration.parent
            )


class FixmeTargets(ErrorSuppressingCommand):
    def __init__(self, arguments: argparse.Namespace, repository: Repository) -> None:
        super().__init__(arguments, repository)
        self._subdirectory: Final[Optional[str]] = arguments.subdirectory
        self._no_commit: bool = arguments.no_commit
        self._submit: bool = arguments.submit
        self._lint: bool = arguments.lint

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        ErrorSuppressingCommand.add_arguments(parser)

    def run(self) -> None:
        subdirectory = self._subdirectory
        subdirectory = Path(subdirectory) if subdirectory else None
        project_configuration = Configuration.find_project_configuration(subdirectory)
        project_directory = project_configuration.parent
        search_root = subdirectory if subdirectory else project_directory

        all_targets = find_targets(search_root)
        if not all_targets:
            return
        for path, targets in all_targets.items():
            self._run_fixme_targets_file(project_directory, path, targets)

        self._repository.submit_changes(
            commit=(not self._no_commit),
            submit=self._submit,
            title=f"Upgrade pyre version for {search_root} (TARGETS)",
        )

    def _run_fixme_targets_file(
        self, project_directory: Path, path: str, targets: List[Target]
    ) -> None:
        LOG.info("Processing %s...", path)
        target_names = [
            path.replace("/TARGETS", "") + ":" + target.name + "-pyre-typecheck"
            for target in targets
        ]
        errors = errors_from_targets(project_directory, path, target_names)
        if not errors:
            return
        LOG.info("Found %d type errors in %s.", len(errors), path)

        if not errors:
            return

        self._suppress_errors(errors)

        if not self._lint:
            return

        if self._repository.format():
            errors = errors_from_targets(project_directory, path, target_names)
            if not errors:
                LOG.info("Errors unchanged after linting.")
                return
            LOG.info("Found %d type errors after linting.", len(errors))
            self._suppress_errors(errors)


def run(repository: Repository) -> None:
    parser = argparse.ArgumentParser(fromfile_prefix_chars="@")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--only-fix-error-code",
        type=int,
        help="Only add fixmes for errors with this specific error code.",
        default=None,
    )

    commands = parser.add_subparsers()

    # Subcommands: Codemods
    missing_overridden_return_annotations = commands.add_parser(
        "missing-overridden-return-annotations",
        help="Add annotations according to errors inputted through stdin.",
    )
    missing_overridden_return_annotations.set_defaults(
        command=MissingOverrideReturnAnnotations
    )

    missing_global_annotations = commands.add_parser(
        "missing-global-annotations",
        help="Add annotations according to errors inputted through stdin.",
    )
    missing_global_annotations.set_defaults(command=MissingGlobalAnnotations)

    # Subcommand: Change default pyre mode to strict and adjust module headers.
    strict_default = commands.add_parser("strict-default")
    StrictDefault.add_arguments(strict_default)

    # Subcommand: Set global configuration to given hash, and add version override
    # to all local configurations to run previous version.
    update_global_version = commands.add_parser("update-global-version")
    update_global_version.set_defaults(command=GlobalVersionUpdate)
    update_global_version.add_argument("hash", help="Hash of new Pyre version")
    update_global_version.add_argument(
        "--paths",
        nargs="*",
        help="A list of paths to local Pyre projects.",
        default=[],
        type=path_exists,
    )
    update_global_version.add_argument(
        "--submit", action="store_true", help=argparse.SUPPRESS
    )

    # Subcommand: Fixme all errors inputted through stdin.
    fixme = commands.add_parser("fixme")
    Fixme.add_arguments(fixme)

    # Subcommand: Fixme all errors for a single project.
    fixme_single = commands.add_parser("fixme-single")
    FixmeSingle.add_arguments(fixme_single)
    fixme_single.set_defaults(command=FixmeSingle)
    fixme_single.add_argument(
        "path", help="Path to project root with local configuration", type=path_exists
    )
    fixme_single.add_argument(
        "--upgrade-version",
        action="store_true",
        help="Upgrade and clean project if a version override set.",
    )
    fixme_single.add_argument(
        "--error-source", choices=["stdin", "generate"], default="generate"
    )
    fixme_single.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_single.add_argument(
        "--no-commit", action="store_true", help=argparse.SUPPRESS
    )
    fixme_single.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)

    # Subcommand: Fixme all errors in all projects with local configurations.
    fixme_all = commands.add_parser("fixme-all")
    FixmeAll.add_arguments(fixme_all)
    fixme_all.set_defaults(command=FixmeAll)
    fixme_all.add_argument(
        "--upgrade-version",
        action="store_true",
        help="Upgrade and clean projects with a version override set.",
    )
    fixme_all.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_all.add_argument("--no-commit", action="store_true", help=argparse.SUPPRESS)
    fixme_all.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)

    # Subcommand: Fixme all errors in targets running type checking
    fixme_targets = commands.add_parser("fixme-targets")
    FixmeTargets.add_arguments(fixme_targets)
    fixme_targets.set_defaults(command=FixmeTargets)
    fixme_targets.add_argument("--submit", action="store_true", help=argparse.SUPPRESS)
    fixme_targets.add_argument("--lint", action="store_true", help=argparse.SUPPRESS)
    fixme_targets.add_argument(
        "--subdirectory", help="Only upgrade TARGETS files within this directory."
    )
    fixme_targets.add_argument(
        "--no-commit", action="store_true", help="Keep changes in working state."
    )

    # Subcommand: Remove targets integration and replace with configuration
    targets_to_configuration = commands.add_parser("targets-to-configuration")
    TargetsToConfiguration.add_arguments(targets_to_configuration)

    # Subcommand: Expand target coverage in configuration up to given error limit
    expand_target_coverage = commands.add_parser("expand-target-coverage")
    ExpandTargetCoverage.add_arguments(expand_target_coverage)

    # Subcommand: Consolidate nested local configurations
    consolidate_nested_configurations = commands.add_parser("consolidate-nested")
    ConsolidateNestedConfigurations.add_arguments(consolidate_nested_configurations)

    # Initialize default values.
    arguments = parser.parse_args()
    if not hasattr(arguments, "command"):
        # Reparsing with `fixme` as default subcommand.
        arguments = parser.parse_args(sys.argv[1:] + ["fixme"])

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.DEBUG if arguments.verbose else logging.INFO,
    )

    try:
        exit_code = ExitCode.SUCCESS
        arguments.command(arguments, repository).run()
    except UnstableAST as error:
        LOG.error(str(error))
        exit_code = ExitCode.FOUND_ERRORS
    except UserError as error:
        LOG.error(str(error))
        exit_code = ExitCode.FAILURE
    except Exception as error:
        LOG.error(str(error))
        LOG.debug(traceback.format_exc())
        exit_code = ExitCode.FAILURE

    sys.exit(exit_code)


def main() -> None:
    run(Repository())


if __name__ == "__main__":
    main()
