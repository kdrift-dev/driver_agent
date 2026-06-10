{ inputs, ... }:
{
  imports = [ inputs.git-hooks-nix.flakeModule ];
  perSystem =
    { config, ... }:
    {
      # Only treefmt (nix + shell). No reuse lint (no SPDX headers in this repo) and
      # no Python hooks, so committing never reformats the original author's code.
      pre-commit = {
        settings = {
          hooks = {
            treefmt = {
              enable = true;
              package = config.treefmt.build.wrapper;
            };
          };
        };
      };
    };
}
