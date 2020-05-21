{ pkgs ? import <nixpkgs> {} }:
with pkgs; let
  my-python-packages = python-packages: with python-packages; [
    requests
  ] ++ [
    # standard dev stuff
    #jedi json-rpc service_factory # anaconda-mode deps
    ipython pytest pylint mypy black
  ];
  python-with-my-packages = python3.withPackages my-python-packages;
in

mkShell {
  buildInputs = [ python-with-my-packages pypi2nix ];
}
