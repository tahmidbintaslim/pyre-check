package com.facebook.buck_project_builder.targets;

import com.facebook.buck_project_builder.BuilderException;
import com.facebook.buck_project_builder.DebugOutput;
import com.facebook.buck_project_builder.FileSystem;
import com.google.common.annotations.VisibleForTesting;
import com.google.common.collect.ImmutableSet;
import org.apache.commons.io.FileUtils;

import java.io.File;
import java.io.IOException;
import java.nio.charset.Charset;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Logger;

public final class BuildTargetsBuilder {

  private static final Logger LOGGER = Logger.getGlobal();

  private final String buckRoot;
  private final String outputDirectory;
  /** key: output path, value: source path */
  private final Map<Path, Path> sources = new HashMap<>();

  private final Set<String> unsupportedGeneratedSources = new HashSet<>();
  private final Set<String> pythonWheelUrls = new HashSet<>();
  private final Set<String> thriftLibraryBuildCommands = new HashSet<>();
  private final Set<String> swigLibraryBuildCommands = new HashSet<>();
  private final Set<String> antlr4LibraryBuildCommands = new HashSet<>();

  private final Set<String> conflictingFiles = new HashSet<>();
  private final Set<String> unsupportedFiles = new HashSet<>();

  public BuildTargetsBuilder(String buckRoot, String outputDirectory) {
    this.buckRoot = buckRoot;
    this.outputDirectory = outputDirectory;
  }

  private static void logCodeGenerationIOException(IOException exception) {
    LOGGER.warning("IOException during python code generation: " + exception.getMessage());
  }

  private void buildPythonSources() {
    LOGGER.info("Building " + this.sources.size() + " python sources...");
    long start = System.currentTimeMillis();
    this.sources
        .entrySet()
        .parallelStream()
        .forEach(mapping -> FileSystem.addSymbolicLink(mapping.getKey(), mapping.getValue()));
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Built python sources in " + time + "ms.");
  }

  private void buildPythonWheels() {
    LOGGER.info("Building " + this.pythonWheelUrls.size() + " python wheels...");
    long start = System.currentTimeMillis();
    File outputDirectoryFile = new File(outputDirectory);
    this.pythonWheelUrls
        .parallelStream()
        .forEach(
            url -> {
              try {
                ImmutableSet<String> conflictingFiles =
                    FileSystem.unzipRemoteFile(url, outputDirectoryFile);
                this.conflictingFiles.addAll(conflictingFiles);
              } catch (IOException firstException) {
                try {
                  ImmutableSet<String> conflictingFiles =
                      FileSystem.unzipRemoteFile(url, outputDirectoryFile);
                  this.conflictingFiles.addAll(conflictingFiles);
                } catch (IOException secondException) {
                  LOGGER.warning(
                      String.format(
                          "Cannot fetch and unzip remote python dependency at `%s` after 1 retry.",
                          url));
                  LOGGER.warning("First IO Exception: " + firstException);
                  LOGGER.warning("Second IO Exception: " + secondException);
                }
              }
            });
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Built python wheels in " + time + "ms.");
  }

  private void runBuildCommands(
      Collection<String> commands, String buildRuleType, CommandRunner commandRunner)
      throws BuilderException {
    int numberOfSuccessfulRuns =
        commands
            .parallelStream()
            .mapToInt(
                command -> {
                  boolean buildIsSuccessful = commandRunner.run(command);
                  if (buildIsSuccessful) {
                    return 1;
                  }
                  LOGGER.severe("Failed to build: " + command);
                  return 0;
                })
            .sum();
    if (numberOfSuccessfulRuns < commands.size()) {
      throw new BuilderException(
          String.format(
              "Failed to build some %s targets. Read the log above for more information.",
              buildRuleType));
    }
  }

  private void buildThriftLibraries() throws BuilderException {
    this.thriftLibraryBuildCommands.removeIf(
        command ->
            command.contains("py:")
                && thriftLibraryBuildCommands.contains(command.replace("py:", "mstch_pyi:")));
    if (this.thriftLibraryBuildCommands.isEmpty()) {
      return;
    }
    int totalNumberOfThriftLibraries = this.thriftLibraryBuildCommands.size();
    LOGGER.info("Building " + totalNumberOfThriftLibraries + " thrift libraries...");
    AtomicInteger numberOfBuiltThriftLibraries = new AtomicInteger(0);
    long start = System.currentTimeMillis();
    runBuildCommands(
        this.thriftLibraryBuildCommands,
        "thrift_library",
        command -> {
          boolean successfullyBuilt;
          try {
            successfullyBuilt = GeneratedBuildRuleRunner.runBuilderCommand(command, this.buckRoot);
          } catch (IOException exception) {
            successfullyBuilt = false;
            logCodeGenerationIOException(exception);
          }
          int builtThriftLibrariesSoFar = numberOfBuiltThriftLibraries.addAndGet(1);
          if (builtThriftLibrariesSoFar % 100 == 0) {
            // Log progress for every 100 built thrift library.
            LOGGER.info(
                String.format(
                    "Built %d/%d thrift libraries.",
                    builtThriftLibrariesSoFar, totalNumberOfThriftLibraries));
          }
          return successfullyBuilt;
        });
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Built thrift libraries in " + time + "ms.");
  }

  private void buildSwigLibraries() throws BuilderException {
    if (this.swigLibraryBuildCommands.isEmpty()) {
      return;
    }
    LOGGER.info("Building " + this.swigLibraryBuildCommands.size() + " swig libraries...");
    String builderExecutable;
    try {
      builderExecutable =
          GeneratedBuildRuleRunner.getBuiltTargetExecutable(
              "//third-party-buck/platform007/tools/swig:bin/swig", this.buckRoot);
    } catch (IOException exception) {
      logCodeGenerationIOException(exception);
      return;
    }
    if (builderExecutable == null) {
      LOGGER.severe("Unable to build any swig libraries because its builder is not found.");
      return;
    }
    long start = System.currentTimeMillis();
    // Swig command contains buck run, so it's better not to make it run in parallel.
    runBuildCommands(
        this.swigLibraryBuildCommands,
        "swig_library",
        command -> {
          try {
            return GeneratedBuildRuleRunner.runBuilderCommand(
                builderExecutable + command, this.buckRoot);
          } catch (IOException exception) {
            logCodeGenerationIOException(exception);
            return false;
          }
        });
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Built swig libraries in " + time + "ms.");
  }

  private void buildAntlr4Libraries() throws BuilderException {
    if (this.antlr4LibraryBuildCommands.isEmpty()) {
      return;
    }
    LOGGER.info("Building " + this.antlr4LibraryBuildCommands.size() + " ANTLR4 libraries...");
    String wrapperExecutable;
    String builderExecutable;
    try {
      wrapperExecutable =
          GeneratedBuildRuleRunner.getBuiltTargetExecutable(
              "//tools/antlr4:antlr4_wrapper", this.buckRoot);
      builderExecutable =
          GeneratedBuildRuleRunner.getBuiltTargetExecutable("//tools/antlr4:antlr4", this.buckRoot);
    } catch (IOException exception) {
      logCodeGenerationIOException(exception);
      return;
    }
    if (builderExecutable == null || wrapperExecutable == null) {
      LOGGER.severe("Unable to build any ANTLR4 libraries because its builder is not found.");
      return;
    }
    String builderPrefix =
        String.format("%s --antlr4_command=\"%s\"", wrapperExecutable, builderExecutable);
    long start = System.currentTimeMillis();
    runBuildCommands(
        this.antlr4LibraryBuildCommands,
        "antlr4_library",
        command -> {
          try {
            return GeneratedBuildRuleRunner.runBuilderCommand(
                builderPrefix + command, this.buckRoot);
          } catch (IOException exception) {
            logCodeGenerationIOException(exception);
            return false;
          }
        });
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Built ANTLR4 libraries in " + time + "ms.");
  }

  private void generateEmptyStubs() {
    LOGGER.info("Generating empty stubs...");
    long start = System.currentTimeMillis();
    Path outputPath = Paths.get(outputDirectory);
    this.unsupportedGeneratedSources
        .parallelStream()
        .forEach(
            source -> {
              File outputFile = new File(source);
              if (outputFile.exists()) {
                // Do not generate stubs for files that has already been handled.
                return;
              }
              if (source.endsWith(".py") && new File(source + "i").exists()) {
                // Do not generate stubs for files if there is already a pyi file for it.
                return;
              }
              String relativeUnsupportedFilename =
                  outputPath.relativize(Paths.get(source)).normalize().toString();
              this.unsupportedFiles.add(relativeUnsupportedFilename);
              outputFile.getParentFile().mkdirs();
              try {
                FileUtils.write(outputFile, "# pyre-placeholder-stub\n", Charset.defaultCharset());
              } catch (IOException exception) {
                logCodeGenerationIOException(exception);
              }
            });
    long time = System.currentTimeMillis() - start;
    LOGGER.info("Generate empty stubs in " + time + "ms.");
  }

  public String getBuckRoot() {
    return buckRoot;
  }

  public String getOutputDirectory() {
    return outputDirectory;
  }

  @VisibleForTesting
  Map<Path, Path> getSources() {
    return sources;
  }

  @VisibleForTesting
  Set<String> getThriftLibraryBuildCommands() {
    return thriftLibraryBuildCommands;
  }

  @VisibleForTesting
  Set<String> getSwigLibraryBuildCommands() {
    return swigLibraryBuildCommands;
  }

  @VisibleForTesting
  Set<String> getAntlr4LibraryBuildCommands() {
    return antlr4LibraryBuildCommands;
  }

  @VisibleForTesting
  Set<String> getUnsupportedGeneratedSources() {
    return unsupportedGeneratedSources;
  }

  @VisibleForTesting
  Set<String> getPythonWheelUrls() {
    return pythonWheelUrls;
  }

  void addSourceMapping(Path sourcePath, Path outputPath) {
    Path existingSourcePath = this.sources.get(outputPath);
    if (existingSourcePath != null && !existingSourcePath.equals(sourcePath)) {
      this.conflictingFiles.add(
          Paths.get(this.outputDirectory).relativize(outputPath).normalize().toString());
      return;
    }
    this.sources.put(outputPath, sourcePath);
  }

  void addUnsupportedGeneratedSource(String generatedSourcePath) {
    unsupportedGeneratedSources.add(generatedSourcePath);
  }

  void addPythonWheelUrl(String url) {
    pythonWheelUrls.add(url);
  }

  void addThriftLibraryBuildCommand(String command) {
    thriftLibraryBuildCommands.add(command);
  }

  void addSwigLibraryBuildCommand(String command) {
    swigLibraryBuildCommands.add(command);
  }

  void addAntlr4LibraryBuildCommand(String command) {
    antlr4LibraryBuildCommands.add(command);
  }

  public DebugOutput buildTargets() throws BuilderException {
    this.buildThriftLibraries();
    this.buildSwigLibraries();
    this.buildAntlr4Libraries();
    this.buildPythonSources();
    this.buildPythonWheels();
    this.generateEmptyStubs();
    return new DebugOutput(this.conflictingFiles, this.unsupportedFiles);
  }

  private interface CommandRunner {
    boolean run(String command);
  }
}
