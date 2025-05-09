# Copyright 2023 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import os
import subprocess
import tempfile
from typing import Optional, Tuple

import distro
import six
from .. import util
from .base import Base

# Please keep these in sync with the packages in README.md
APT_PKGS = ['git', 'curl', 'autoconf', 'libx11-dev', 'libfreetype6-dev',
            'libgl1-mesa-dri', 'libglib2.0-dev', 'xorg-dev', 'gperf', 'g++',
            'build-essential', 'cmake', 'libssl-dev',
            'liblzma-dev', 'libxmu6', 'libxmu-dev',
            "libxcb-render0-dev", "libxcb-shape0-dev", "libxcb-xfixes0-dev",
            'libgles2-mesa-dev', 'libegl1-mesa-dev', 'libdbus-1-dev',
            'libharfbuzz-dev', 'ccache', 'clang', 'libunwind-dev',
            'libgstreamer1.0-dev', 'libgstreamer-plugins-base1.0-dev',
            'libgstreamer-plugins-bad1.0-dev', 'autoconf2.13',
            'libunwind-dev', 'llvm-dev']
DNF_PKGS = ['libtool', 'gcc-c++', 'libXi-devel', 'freetype-devel',
            'libunwind-devel', 'mesa-libGL-devel', 'mesa-libEGL-devel',
            'glib2-devel', 'libX11-devel', 'libXrandr-devel', 'gperf',
            'fontconfig-devel', 'cabextract', 'ttmkfdir', 'expat-devel',
            'rpm-build', 'openssl-devel', 'cmake',
            'libXcursor-devel', 'libXmu-devel',
            'dbus-devel', 'ncurses-devel', 'harfbuzz-devel', 'ccache',
            'clang', 'clang-libs', 'llvm', 'autoconf213', 'python3-devel',
            'gstreamer1-devel', 'gstreamer1-plugins-base-devel',
            'gstreamer1-plugins-bad-free-devel', 'libjpeg-turbo-devel',
            'zlib', 'libjpeg']
XBPS_PKGS = ['libtool', 'gcc', 'libXi-devel', 'freetype-devel',
             'libunwind-devel', 'MesaLib-devel', 'glib-devel', 'pkg-config',
             'libX11-devel', 'libXrandr-devel', 'gperf', 'bzip2-devel',
             'fontconfig-devel', 'cabextract', 'expat-devel', 'cmake',
             'cmake', 'libXcursor-devel', 'libXmu-devel', 'dbus-devel',
             'ncurses-devel', 'harfbuzz-devel', 'ccache', 'glu-devel',
             'clang', 'gstreamer1-devel', 'autoconf213',
             'gst-plugins-base1-devel', 'gst-plugins-bad1-devel']

GSTREAMER_URL = \
    "https://github.com/servo/servo-build-deps/releases/download/linux/gstreamer-1.16-x86_64-linux-gnu.20190515.tar.gz"
PREPACKAGED_GSTREAMER_ROOT = \
    os.path.join(util.get_target_dir(), "dependencies", "gstreamer")


class Linux(Base):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_linux = True
        (self.distro, self.version) = Linux.get_distro_and_version()

    def library_path_variable_name(self):
        return "LD_LIBRARY_PATH"

    @staticmethod
    def get_distro_and_version() -> Tuple[str, str]:
        distrib = six.ensure_str(distro.name())
        version = six.ensure_str(distro.version())

        if distrib in ['LinuxMint', 'Linux Mint', 'KDE neon', 'Pop!_OS']:
            if '.' in version:
                major, _ = version.split('.', 1)
            else:
                major = version

            distrib = 'Ubuntu'
            if major == '22':
                version = '22.04'
            elif major == '21':
                version = '21.04'
            elif major == '20':
                version = '20.04'
            elif major == '19':
                version = '18.04'
            elif major == '18':
                version = '16.04'

        if distrib.lower() == 'elementary':
            distrib = 'Ubuntu'
            if version == '5.0':
                version = '18.04'
            elif version[0:3] == '0.4':
                version = '16.04'

        return (distrib, version)

    def _platform_bootstrap(self, _cache_dir: str, force: bool) -> bool:
        if self.distro.lower() == 'nixos':
            print('NixOS does not need bootstrap, it will automatically enter a nix-shell')
            print('Just run ./mach build')
            print('')
            print('You will need to run a nix-shell if you are trying '
                  'to run any of the built binaries')
            print('To enter the nix-shell manually use:')
            print('  $ nix-shell etc/shell.nix')
            return False

        if self.distro.lower() == 'ubuntu' and self.version > '22.04':
            print(f"WARNING: unsupported version of {self.distro}: {self.version}")

        # FIXME: Better version checking for these distributions.
        if self.distro.lower() not in [
            'arch linux',
            'arch',
            'centos linux',
            'centos',
            'debian gnu/linux',
            'fedora linux',
            'fedora',
            'nixos',
            'ubuntu',
            'void',
        ]:
            raise NotImplementedError("mach bootstrap does not support "
                                      f"{self.distro}, please file a bug")

        installed_something = self.install_non_gstreamer_dependencies(force)
        installed_something |= self._platform_bootstrap_gstreamer(force)
        return installed_something

    def install_non_gstreamer_dependencies(self, force: bool) -> bool:
        install = False
        pkgs = []
        if self.distro in ['Ubuntu', 'Debian GNU/Linux']:
            command = ['apt-get', 'install']
            pkgs = APT_PKGS
            if subprocess.call(['dpkg', '-s'] + pkgs,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE) != 0:
                install = True
        elif self.distro in ['CentOS', 'CentOS Linux', 'Fedora', 'Fedora Linux']:
            installed_pkgs = str(subprocess.check_output(['rpm', '-qa'])).replace('\n', '|')
            pkgs = DNF_PKGS
            for pkg in pkgs:
                command = ['dnf', 'install']
                if "|{}".format(pkg) not in installed_pkgs:
                    install = True
                    break
        elif self.distro == 'void':
            installed_pkgs = str(subprocess.check_output(['xbps-query', '-l']))
            pkgs = XBPS_PKGS
            for pkg in pkgs:
                command = ['xbps-install', '-A']
                if "ii {}-".format(pkg) not in installed_pkgs:
                    install = force = True
                    break

        if not install:
            return False

        def run_as_root(command, force=False):
            if os.geteuid() != 0:
                command.insert(0, 'sudo')
            if force:
                command.append('-y')
            return subprocess.call(command)

        print("Installing missing dependencies...")
        if run_as_root(command + pkgs, force) != 0:
            raise EnvironmentError("Installation of dependencies failed.")
        return True

    def gstreamer_root(self, cross_compilation_target: Optional[str]) -> Optional[str]:
        if cross_compilation_target:
            return None
        if os.path.exists(PREPACKAGED_GSTREAMER_ROOT):
            return PREPACKAGED_GSTREAMER_ROOT
        # GStreamer might be installed system-wide, but we do not return a root in this
        # case because we don't have to update environment variables.
        return None

    def _platform_bootstrap_gstreamer(self, force: bool) -> bool:
        if not force and self.is_gstreamer_installed(cross_compilation_target=None):
            return False

        with tempfile.TemporaryDirectory() as temp_dir:
            file_name = os.path.join(temp_dir, GSTREAMER_URL.rsplit('/', maxsplit=1)[-1])
            util.download_file("Pre-packaged GStreamer binaries", GSTREAMER_URL, file_name)

            print(f"Installing GStreamer packages to {PREPACKAGED_GSTREAMER_ROOT}...")
            os.makedirs(PREPACKAGED_GSTREAMER_ROOT, exist_ok=True)

            # Extract, but strip one component from the output, because the package includes
            # a toplevel directory called "./gst/" and we'd like to have the same directory
            # structure on all platforms.
            subprocess.check_call(["tar", "xf", file_name, "-C", PREPACKAGED_GSTREAMER_ROOT,
                                   "--strip-components=2"])

            assert self.is_gstreamer_installed(cross_compilation_target=None)
            return True
