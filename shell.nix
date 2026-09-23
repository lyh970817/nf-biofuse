{ pkgs ? import <nixpkgs> {} }:
let
  # The Nextflow release the plugin is developed and verified against. Keep it in step with the
  # nf-core/gwas dev shell, which pins the same launcher the same way.
  nextflowVersion = "26.04.6";

  nextflowCli = pkgs.stdenvNoCC.mkDerivation {
    pname = "nextflow";
    version = nextflowVersion;
    src = pkgs.fetchurl {
      url = "https://github.com/nextflow-io/nextflow/releases/download/v${nextflowVersion}/nextflow";
      sha256 = "10c2jqc17rcm8b6gyvadd8pilj38mxknqflgasxwyhyppvnmb9v1";
    };
    dontUnpack = true;
    installPhase = ''
      install -Dm755 "$src" "$out/bin/nextflow"
    '';
  };

  # `conda` for the supervisor environment, backed by micromamba. The environment lives wherever
  # `-p` puts it; MAMBA_ROOT_PREFIX only holds micromamba's package cache.
  condaCli = pkgs.writeShellApplication {
    name = "conda";
    runtimeInputs = [ pkgs.micromamba ];
    text = ''
      export MAMBA_ROOT_PREFIX="''${MAMBA_ROOT_PREFIX:-$HOME/.cache/micromamba}"
      exec micromamba --yes "$@"
    '';
  };
in
pkgs.mkShell {
  packages = [
    pkgs.bash
    pkgs.coreutils
    pkgs.gnumake
    pkgs.git
    # The Nextflow Gradle plugin compiles with a Java 21 toolchain (to Java 17 bytecode) and finds it
    # through JAVA_HOME; Nextflow itself runs on the same JDK.
    pkgs.jdk21_headless
    nextflowCli
    condaCli
    pkgs.micromamba
    # The local FUSE tier: Apptainer consumers, tabix to build a store from a VCF, curl for images.
    pkgs.apptainer
    pkgs.htslib
    pkgs.curl
    pkgs.procps
    pkgs.util-linux
  ];
  shellHook = ''
    export NXF_VER="${nextflowVersion}"
    export JAVA_HOME="${pkgs.jdk21_headless.home}"
    if [[ $- == *i* && -z "''${DIRENV_IN_ENVRC:-}" && "''${SHELL:-}" != */bash ]]; then
      exec bash
    fi
  '';
}
