{ pkgs ? import <nixpkgs> {} }:
with pkgs; let
  mach-nix = import (builtins.fetchGit {
    url = "https://github.com/DavHau/mach-nix/";
    ref ="refs/tags/3.3.0";
  }) {};

  python-env = mach-nix.mkPython {
    requirements = lib.concatMapStrings builtins.readFile [
      ./requirements.txt
      ./requirements-dev.txt
    ];
  };
in

mkShell {
  buildInputs = [ python-env ];
}
