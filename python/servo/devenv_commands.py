# Copyright 2013 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

from __future__ import print_function, unicode_literals
from os import path, listdir, getcwd

import signal
import subprocess
import sys
import tempfile
import urllib

from mach.decorators import (
    CommandArgument,
    CommandProvider,
    Command,
)

from servo.command_base import CommandBase, cd, call
from servo.util import get_static_rust_lang_org_dist, get_urlopen_kwargs


@CommandProvider
class MachCommands(CommandBase):
    @Command('check',
             description='Run "cargo check"',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help="Command-line arguments to be passed through to cargo check")
    @CommandBase.build_like_command_arguments
    def check(self, params, features=[], media_stack=None, target=None,
              android=False, **kwargs):
        if not params:
            params = []

        features = features or []

        target, android = self.pick_target_triple(target, android)

        features += self.pick_media_stack(media_stack, target)

        self.ensure_bootstrapped(target=target)
        self.ensure_clobbered()

        status = self.run_cargo_build_like_command("check", params, features=features, **kwargs)
        if status == 0:
            print('Finished checking, binary NOT updated. Consider ./mach build before ./mach run')

        return status

    @Command('cargo-update',
             description='Same as update-cargo',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help='Command-line arguments to be passed through to cargo update')
    @CommandArgument(
        '--package', '-p', default=None,
        help='Updates selected package')
    @CommandArgument(
        '--all-packages', '-a', action='store_true',
        help='Updates all packages')
    @CommandArgument(
        '--dry-run', '-d', action='store_true',
        help='Show outdated packages.')
    def cargo_update(self, params=None, package=None, all_packages=None, dry_run=None):
        self.update_cargo(params, package, all_packages, dry_run)

    @Command('update-cargo',
             description='Update Cargo dependencies',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help='Command-line arguments to be passed through to cargo update')
    @CommandArgument(
        '--package', '-p', default=None,
        help='Updates the selected package')
    @CommandArgument(
        '--all-packages', '-a', action='store_true',
        help='Updates all packages. NOTE! This is very likely to break your '
             'working copy, making it impossible to build servo. Only do '
             'this if you really know what you are doing.')
    @CommandArgument(
        '--dry-run', '-d', action='store_true',
        help='Show outdated packages.')
    def update_cargo(self, params=None, package=None, all_packages=None, dry_run=None):
        if not params:
            params = []

        if not package and not all_packages:
            print("Please choose package to update with the --package (-p) ")
            print("flag or update all packages with --all-packages (-a) flag")
            sys.exit(1)

        if package:
            params += ["-p", package]
        if dry_run:
            params.append("--dry-run")

        self.ensure_bootstrapped()
        with cd(self.context.topdir):
            self.call_rustup_run(["cargo", "update"] + params, env=self.build_env())

    @Command('rustc',
             description='Run the Rust compiler',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help="Command-line arguments to be passed through to rustc")
    def rustc(self, params):
        if params is None:
            params = []

        self.ensure_bootstrapped()
        return self.call_rustup_run(["rustc"] + params, env=self.build_env())

    @Command('cargo-fix',
             description='Run "cargo fix"',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help="Command-line arguments to be passed through to cargo-fix")
    @CommandBase.build_like_command_arguments
    def cargo_fix(self, params, features=[], media_stack=None, target=None,
                  android=False, **kwargs):
        if not params:
            params = []

        features = features or []

        target, android = self.pick_target_triple(target, android)

        features += self.pick_media_stack(media_stack, target)

        self.ensure_bootstrapped(target=target)
        self.ensure_clobbered()

        return self.run_cargo_build_like_command("fix", params, features=features, **kwargs)

    @Command('cargo-clippy',
             description='Run "cargo clippy"',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help="Command-line arguments to be passed through to cargo-clippy")
    @CommandBase.build_like_command_arguments
    def cargo_clippy(self, params, features=[], media_stack=None, target=None,
                     android=False, **kwargs):
        if not params:
            params = []

        features = features or []

        target, android = self.pick_target_triple(target, android)

        features += self.pick_media_stack(media_stack, target)

        self.ensure_bootstrapped(target=target)
        self.ensure_clobbered()

        return self.run_cargo_build_like_command("clippy", params, features=features, **kwargs)

    @Command('grep',
             description='`git grep` for selected directories.',
             category='devenv')
    @CommandArgument(
        'params', default=None, nargs='...',
        help="Command-line arguments to be passed through to `git grep`")
    def grep(self, params):
        if not params:
            params = []
        # get all directories under tests/
        tests_dirs = listdir('tests')
        # Directories to be excluded under tests/
        excluded_tests_dirs = ['wpt', 'jquery']
        tests_dirs = filter(lambda dir: dir not in excluded_tests_dirs, tests_dirs)
        # Set of directories in project root
        root_dirs = ['components', 'ports', 'python', 'etc', 'resources']
        # Generate absolute paths for directories in tests/ and project-root/
        tests_dirs_abs = [path.join(self.context.topdir, 'tests', s) for s in tests_dirs]
        root_dirs_abs = [path.join(self.context.topdir, s) for s in root_dirs]
        # Absolute paths for all directories to be considered
        grep_paths = root_dirs_abs + tests_dirs_abs
        return call(
            ["git"] + ["grep"] + params + ['--'] + grep_paths + [':(exclude)*.min.js', ':(exclude)*.min.css'],
            env=self.build_env())

    @Command('rustup',
             description='Update the Rust version to latest Nightly',
             category='devenv')
    def rustup(self):
        url = get_static_rust_lang_org_dist() + "/channel-rust-nightly-date.txt"
        nightly_date = urllib.request.urlopen(url, **get_urlopen_kwargs()).read()
        toolchain = b"nightly-" + nightly_date
        filename = path.join(self.context.topdir, "rust-toolchain")
        with open(filename, "wb") as f:
            f.write(toolchain + b"\n")
        self.ensure_bootstrapped()

    @Command('fetch',
             description='Fetch Rust, Cargo and Cargo dependencies',
             category='devenv')
    def fetch(self):
        self.ensure_bootstrapped()

        with cd(self.context.topdir):
            return self.call_rustup_run(["cargo", "fetch"], env=self.build_env())

    @Command('ndk-stack',
             description='Invoke the ndk-stack tool with the expected symbol paths',
             category='devenv')
    @CommandArgument('--release', action='store_true', help="Use release build symbols")
    @CommandArgument('--target', action='store', default="armv7-linux-androideabi",
                     help="Build target")
    @CommandArgument('logfile', action='store', help="Path to logcat output with crash report")
    def stack(self, release, target, logfile):
        if not path.isfile(logfile):
            print(logfile + " doesn't exist")
            return -1
        env = self.build_env(target=target)
        ndk_stack = path.join(env["ANDROID_NDK"], "ndk-stack")
        self.handle_android_target(target)
        sym_path = path.join(
            "target",
            target,
            "release" if release else "debug",
            "apk",
            "obj",
            "local",
            self.config["android"]["lib"])
        print(subprocess.check_output([ndk_stack, "-sym", sym_path, "-dump", logfile]))

    @Command('ndk-gdb',
             description='Invoke ndk-gdb tool with the expected symbol paths',
             category='devenv')
    @CommandArgument('--release', action='store_true', help="Use release build symbols")
    @CommandArgument('--target', action='store', default="armv7-linux-androideabi",
                     help="Build target")
    def ndk_gdb(self, release, target):
        env = self.build_env(target)
        self.handle_android_target(target)
        ndk_gdb = path.join(env["ANDROID_NDK"], "ndk-gdb")
        adb_path = path.join(env["ANDROID_SDK"], "platform-tools", "adb")
        sym_paths = [
            path.join(
                getcwd(),
                "target",
                target,
                "release" if release else "debug",
                "apk",
                "obj",
                "local",
                self.config["android"]["lib"]
            ),
            path.join(
                getcwd(),
                "target",
                target,
                "release" if release else "debug",
                "apk",
                "libs",
                self.config["android"]["lib"]
            ),
        ]
        env["NDK_PROJECT_PATH"] = path.join(getcwd(), "support", "android", "apk")
        signal.signal(signal.SIGINT, signal.SIG_IGN)

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write('\n'.join([
                "python",
                "param = gdb.parameter('solib-search-path')",
                "param += ':{}'".format(':'.join(sym_paths)),
                "gdb.execute('set solib-search-path ' + param)",
                "end",
            ]))

        p = subprocess.Popen([
            ndk_gdb,
            "--adb", adb_path,
            "--project", "support/android/apk/servoapp/src/main/",
            "--launch", "org.mozilla.servo.MainActivity",
            "-x", f.name,
            "--verbose",
        ], env=env)
        return p.wait()

    @Command('try',
             description='Runs try jobs by force pushing to personal fork try branches',
             category='devenv')
    @CommandArgument(
        'jobs', default=["try"], nargs='...',
        help="Name(s) of job(s) (ex: try, linux, mac, windows, wpt)")
    def try_jobs(self, jobs):
        branches = []
        # we validate branches because force pushing is destructive
        VALID_TRY_BRACHES = ["try", "try-linux", "try-mac", "try-windows", "try-wpt", "try-wpt-2020"]
        for job in jobs:
            # branches must start with try-
            if "try" not in job:
                job = "try-" + job
            if job not in VALID_TRY_BRACHES:
                print(job + " job doesn't exist")
                return -1
            branches.append(job)
        remote = "origin"
        if "servo/servo" in subprocess.check_output(["git", "config", "--get", "remote.origin.url"]).decode():
            # if we have servo/servo for origin check try remote
            try:
                if "servo/servo" in subprocess.check_output(["git", "config", "--get", "remote.try.url"]).decode():
                    # User has servo/servo for try remote
                    print("You should not use servo/servo for try remote!")
                    return -1
                else:
                    remote = "try"
            except subprocess.CalledProcessError:
                print("It looks like you are patching in upstream servo.")
                print("Set try remote to your personal fork with `git remote add try https://github.com/user/servo`")
                return -1
        for b in branches:
            res = call(["git", "push", remote, "--force", f"HEAD:{b}"], env=self.build_env())
            if res != 0:
                return res
        return 0
