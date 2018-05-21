# Copyright (c) 2016-present, Facebook, Inc.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import functools
import json
import logging
import os
import shutil
from typing import List

from . import (
    BINARY_NAME,
    CONFIGURATION_FILE,
    EnvironmentException,
    find_typeshed,
    number_of_workers,
)


LOG = logging.getLogger(__name__)


class InvalidConfiguration(Exception):
    pass


class Configuration:
    _disabled = False  # type: bool

    def __init__(
        self,
        original_directory=None,
        local_configuration=None,
        search_path=None,
        typeshed=None,
        preserve_pythonpath=False,
    ) -> None:
        self.source_directories = []
        self.targets = []
        self.logger = None
        self.autogenerated = []
        self.number_of_workers = None

        self._version_hash = None
        self._binary = None
        self._typeshed = None

        # Handle search path from multiple sources
        self._search_directories = []
        pythonpath = os.getenv("PYTHONPATH")
        if preserve_pythonpath and pythonpath:
            for path in pythonpath.split(":"):
                if os.path.isdir(path):
                    self._search_directories.append(path)
                else:
                    LOG.warning(
                        "`{}` is not a valid directory, dropping it "
                        "from PYTHONPATH".format(path)
                    )
        if search_path:
            self._search_directories.extend(search_path)
        # We will extend the search path further, with the config file
        # items, inside _read().

        if typeshed:
            self._typeshed = typeshed

        # Order matters. The values will only be updated if a field is None.
        local_configuration = local_configuration or original_directory
        if local_configuration:
            if not os.path.isfile(local_configuration):
                local_configuration = os.path.join(
                    local_configuration, CONFIGURATION_FILE + ".local"
                )
            self._read(local_configuration)
        self._read(CONFIGURATION_FILE + ".local")
        self._read(CONFIGURATION_FILE)
        self._resolve_versioned_paths()
        self._apply_defaults()

    def validate(self) -> None:
        try:

            def is_list_of_strings(list):
                if len(list) == 0:
                    return True
                return not isinstance(list, str) and all(
                    isinstance(element, str) for element in list
                )

            if not is_list_of_strings(
                self.source_directories
            ) or not is_list_of_strings(self.targets):
                raise InvalidConfiguration(
                    "`target` and `source_directories` fields must be lists of "
                    "strings."
                )

            if not is_list_of_strings(self.autogenerated):
                raise InvalidConfiguration(
                    "`autogenerated` field must be a list of strings."
                )

            if not self._binary:
                raise InvalidConfiguration("`binary` location must be defined")
            if not os.path.exists(self.get_binary()):
                raise InvalidConfiguration(
                    "Binary at `{}` does not exist".format(self._binary)
                )

            if self.number_of_workers < 1:
                raise InvalidConfiguration("Number of workers must be greater than 0")

            # Validate elements of the search path.
            if not self._typeshed:
                raise InvalidConfiguration("`typeshed` location must be defined")

            # Validate typeshed path
            typeshed_subdirectories = os.listdir(self._typeshed)
            if "stdlib" in typeshed_subdirectories:
                self._typeshed = os.path.join(self._typeshed, "stdlib/")
                typeshed_subdirectories = os.listdir(self._typeshed)
            for typeshed_version_directory in os.listdir(self._typeshed):
                if not typeshed_version_directory[0].isdigit():
                    raise InvalidConfiguration(
                        "`typeshed` location must contain a stdlib directory which "
                        "only contains subdirectories starting with a version number."
                    )
            for path in self.get_search_path():
                if not os.path.isdir(path):
                    raise InvalidConfiguration(
                        "`{}` is not a valid directory".format(path)
                    )
        except InvalidConfiguration as error:
            raise EnvironmentException("Invalid configuration: {}.".format(str(error)))

    def get_version_hash(self):
        return self._version_hash

    @functools.lru_cache(1)
    def get_binary(self):
        if not self._binary:
            raise InvalidConfiguration("Configuration was not validated")

        return self._binary

    @functools.lru_cache(1)
    def get_search_path(self) -> List[str]:
        if not self._typeshed:
            raise InvalidConfiguration("Configuration was not validated")

        if self._typeshed in self._search_directories:
            # Avoid redundant lookups.
            return self._search_directories
        else:
            return self._search_directories + [self._typeshed]

    def disabled(self) -> bool:
        return self._disabled

    def _read(self, path) -> None:
        try:
            with open(path) as file:
                LOG.debug("Reading configuration `%s`...", path)

                configuration = json.load(file)

                if not self.source_directories:
                    self.source_directories = configuration.get(
                        "source_directories", []
                    )
                if not self.source_directories:
                    self.source_directories = configuration.get("link_trees", [])
                if self.source_directories:
                    LOG.debug(
                        "Found source directories `%s`",
                        ", ".join(self.source_directories),
                    )

                if not self.targets:
                    self.targets = configuration.get("targets", [])
                if self.targets:
                    LOG.debug("Found targets `%s`", ", ".join(self.targets))

                if "disabled" in configuration:
                    self._disabled = True

                if not self.logger:
                    self.logger = configuration.get("logger")

                if not self.autogenerated:
                    self.autogenerated = configuration.get("autogenerated", [])

                if not self.number_of_workers:
                    self.number_of_workers = int(configuration.get("workers", 0))

                if not self._binary:
                    self._binary = configuration.get("binary")

                self._search_directories.extend(configuration.get("search_path", []))

                if not self._version_hash:
                    self._version_hash = configuration.get("version")

                if not self._typeshed:
                    self._typeshed = configuration.get("typeshed")
        except IOError:
            LOG.debug("No configuration found at `{}`.".format(path))
        except json.JSONDecodeError as error:
            raise EnvironmentException(
                "Configuration file at `{}` is invalid: {}.".format(path, str(error))
            )

    def _resolve_versioned_paths(self) -> None:
        version_hash = self.get_version_hash()
        binary = self._binary
        if version_hash and binary:
            self._binary = binary.replace("%V", version_hash)
        if version_hash and self._typeshed:
            self._typeshed = self._typeshed.replace("%V", version_hash)

    def _apply_defaults(self) -> None:
        if not self.source_directories:
            self.source_directories.append(".")
            LOG.info("No source directory specified, using current directory")

        overriding_binary = os.getenv("PYRE_BINARY")
        if overriding_binary:
            self._binary = overriding_binary
            LOG.warning("Binary overridden with `%s`", self._binary)
        if not self._binary:
            LOG.info(
                "No binary specified, looking for `{}` in PATH".format(BINARY_NAME)
            )
            self._binary = shutil.which(BINARY_NAME)
            if not self._binary:
                LOG.warning("Could not find `{}` in PATH".format(BINARY_NAME))
            else:
                LOG.info("Found: `%s`", self._binary)

        overriding_version_hash = os.getenv("PYRE_VERSION_HASH")
        if overriding_version_hash:
            self._version_hash = overriding_version_hash
            LOG.warning("Version hash overridden with `%s`", self._version_hash)

        if not self.number_of_workers:
            self.number_of_workers = number_of_workers()

        if not self._typeshed:
            LOG.info("No typeshed specified, looking for it")
            self._typeshed = find_typeshed()
            if not self._typeshed:
                LOG.warning("Could not find a suitable typeshed")
            else:
                LOG.info("Found: `%s`", self._typeshed)
