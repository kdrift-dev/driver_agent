{ inputs, ... }:
{
  imports = [ inputs.devshell.flakeModule ];

  perSystem =
    {
      pkgs,
      config,
      system,
      ...
    }:
    {
      devshells.default = {
        name = "Driver-Agent devshell";

        packages = builtins.attrValues {
          # Python: openai SDK is only strictly needed for the `openai` provider,
          # but include it so all providers work out of the box.
          python = pkgs.python3.withPackages (ps: [ ps.openai ]);

          # Core pipeline + analysis tooling.
          inherit (pkgs)
            git
            ripgrep
            gnumake
            gcc
            binutils
            pkg-config
            bash
            coreutils
            rsync
            kmod
            ;

          # GNU userland the kernel build expects (busybox applets are NOT used on
          # PATH — busybox is reached only via DRIVER_AGENT_BUSYBOX for the initramfs).
          inherit (pkgs)
            gawk
            gnugrep
            gnused
            findutils
            diffutils
            gzip
            xz
            which
            ;

          # Kernel build deps (make modules_prepare + module compile).
          inherit (pkgs)
            bc
            bison
            flex
            openssl
            elfutils
            ncurses
            perl
            gmp
            libmpc
            mpfr
            zlib
            ;

          # QEMU/kselftest/KUnit runtime hooks. (busybox is provided via the
          # DRIVER_AGENT_BUSYBOX env var below, not on PATH, to avoid its applet
          # symlinks shadowing the GNU userland during kernel builds.)
          inherit (pkgs)
            qemu
            cpio
            gnutar
            ;

          # Dev tooling.
          inherit (inputs.nix-fast-build.packages.${system}) nix-fast-build;
          treefmt = config.treefmt.build.wrapper;
        };

        env = [
          # Consumed by the busybox override in the QEMU tools. MUST be a static
          # build: the initramfs builders copy only this single binary (no glibc
          # loader / shared libs / nix store closure), so a dynamically-linked
          # busybox would fail to exec /bin/sh inside the guest. pkgsStatic.busybox
          # is a musl static binary that runs standalone.
          {
            name = "DRIVER_AGENT_BUSYBOX";
            value = "${pkgs.pkgsStatic.busybox}/bin/busybox";
          }
          # Default Linux source tree for reproduction (overridable).
          {
            name = "LINUX_REPO";
            value = "/home/humaid/repos/linux";
          }
          # Kernel host tools (objtool, resolve_btfids, sign-file) find libelf /
          # openssl / zlib via pkg-config; point it at the dev outputs.
          {
            name = "PKG_CONFIG_PATH";
            prefix = "${pkgs.elfutils.dev}/lib/pkgconfig:${pkgs.openssl.dev}/lib/pkgconfig:${pkgs.zlib.dev}/lib/pkgconfig";
          }
          # Fallback for the few host tools that #include <gelf.h> without pkg-config.
          # Scoped to libelf only to minimise leakage into kernel object compiles.
          {
            name = "C_INCLUDE_PATH";
            prefix = "${pkgs.elfutils.dev}/include";
          }
          {
            name = "LIBRARY_PATH";
            prefix = "${pkgs.elfutils.out}/lib";
          }
        ];

        commands = [
          {
            name = "da-dry-run";
            help = "Run a single case through stages 1-9 (no LLM, no compile)";
            command = "MODE=dry exec scripts/repro_case.sh \"$@\"";
          }
          {
            name = "da-run";
            help = "Run a single case with OpenRouter LLM + module compile";
            command = "MODE=llm exec scripts/repro_case.sh \"$@\"";
          }
        ];
      };
    };
}
