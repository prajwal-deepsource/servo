# Copyright 2013 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

from __future__ import print_function, unicode_literals

import argparse
import re
import sys
import os
import os.path as path
import copy
from collections import OrderedDict
import time
import shutil
import subprocess
from xml.etree.ElementTree import XML
from six import iteritems

import wpt
import wpt.manifestupdate
import wpt.run
import wpt.update

from mach.registrar import Registrar
from mach.decorators import (
    CommandArgument,
    CommandProvider,
    Command,
)

from servo_tidy import tidy
from servo.command_base import (
    CommandBase,
    call, check_call, check_output,
)
from servo_tidy_tests import test_tidy

SCRIPT_PATH = os.path.split(__file__)[0]
PROJECT_TOPLEVEL_PATH = os.path.abspath(os.path.join(SCRIPT_PATH, "..", ".."))
WEB_PLATFORM_TESTS_PATH = os.path.join("tests", "wpt", "web-platform-tests")
SERVO_TESTS_PATH = os.path.join("tests", "wpt", "mozilla", "tests")

CLANGFMT_CPP_DIRS = ["support/hololens/"]
CLANGFMT_VERSION = "15"

TEST_SUITES = OrderedDict([
    ("tidy", {"kwargs": {"all_files": False, "no_progress": False, "self_test": False,
                         "stylo": False},
              "include_arg": "include"}),
    ("wpt", {"kwargs": {"release": False},
             "paths": [path.abspath(WEB_PLATFORM_TESTS_PATH),
                       path.abspath(SERVO_TESTS_PATH)],
             "include_arg": "include"}),
    ("unit", {"kwargs": {},
              "paths": [path.abspath(path.join("tests", "unit"))],
              "include_arg": "test_name"}),
])

TEST_SUITES_BY_PREFIX = {path: k for k, v in iteritems(TEST_SUITES) if "paths" in v for path in v["paths"]}


@CommandProvider
class MachCommands(CommandBase):
    DEFAULT_RENDER_MODE = "cpu"
    HELP_RENDER_MODE = "Value can be 'cpu', 'gpu' or 'both' (default " + DEFAULT_RENDER_MODE + ")"

    def __init__(self, context):
        CommandBase.__init__(self, context)
        if not hasattr(self.context, "built_tests"):
            self.context.built_tests = False

    @Command('test',
             description='Run specified Servo tests',
             category='testing')
    @CommandArgument('params', default=None, nargs="...",
                     help="Optionally select test based on "
                          "test file directory")
    @CommandArgument('--render-mode', '-rm', default=DEFAULT_RENDER_MODE,
                     help="The render mode to be used on all tests. "
                          + HELP_RENDER_MODE)
    @CommandArgument('--release', default=False, action="store_true",
                     help="Run with a release build of servo")
    @CommandArgument('--tidy-all', default=False, action="store_true",
                     help="Check all files, and run the WPT lint in tidy, "
                          "even if unchanged")
    @CommandArgument('--no-progress', default=False, action="store_true",
                     help="Don't show progress for tidy")
    @CommandArgument('--self-test', default=False, action="store_true",
                     help="Run unit tests for tidy")
    @CommandArgument('--all', default=False, action="store_true", dest="all_suites",
                     help="Run all test suites")
    def test(self, params, render_mode=DEFAULT_RENDER_MODE, release=False, tidy_all=False,
             no_progress=False, self_test=False, all_suites=False):
        suites = copy.deepcopy(TEST_SUITES)
        suites["tidy"]["kwargs"] = {"all_files": tidy_all, "no_progress": no_progress, "self_test": self_test,
                                    "stylo": False}
        suites["wpt"]["kwargs"] = {"release": release}
        suites["unit"]["kwargs"] = {}

        selected_suites = OrderedDict()

        if params is None:
            if all_suites:
                params = suites.keys()
            else:
                print("Specify a test path or suite name, or pass --all to run all test suites.\n\nAvailable suites:")
                for s in suites:
                    print("    %s" % s)
                return 1

        for arg in params:
            found = False
            if arg in suites and arg not in selected_suites:
                selected_suites[arg] = []
                found = True
            else:
                suite = self.suite_for_path(arg)
                if suite is not None:
                    if suite not in selected_suites:
                        selected_suites[suite] = []
                    selected_suites[suite].append(arg)
                    found = True
                    break

            if not found:
                print("%s is not a valid test path or suite name" % arg)
                return 1

        test_start = time.time()
        for suite, tests in iteritems(selected_suites):
            props = suites[suite]
            kwargs = props.get("kwargs", {})
            if tests:
                kwargs[props["include_arg"]] = tests

            Registrar.dispatch("test-%s" % suite, context=self.context, **kwargs)

        elapsed = time.time() - test_start

        print("Tests completed in %0.2fs" % elapsed)

    # Helper to determine which test suite owns the path
    def suite_for_path(self, path_arg):
        if os.path.exists(path.abspath(path_arg)):
            abs_path = path.abspath(path_arg)
            for prefix, suite in iteritems(TEST_SUITES_BY_PREFIX):
                if abs_path.startswith(prefix):
                    return suite
        return None

    @Command('test-perf',
             description='Run the page load performance test',
             category='testing')
    @CommandArgument('--base', default=None,
                     help="the base URL for testcases")
    @CommandArgument('--date', default=None,
                     help="the datestamp for the data")
    @CommandArgument('--submit', '-a', default=False, action="store_true",
                     help="submit the data to perfherder")
    def test_perf(self, base=None, date=None, submit=False):
        env = self.build_env()
        cmd = ["bash", "test_perf.sh"]
        if base:
            cmd += ["--base", base]
        if date:
            cmd += ["--date", date]
        if submit:
            cmd += ["--submit"]
        return call(cmd,
                    env=env,
                    cwd=path.join("etc", "ci", "performance"))

    @Command('test-unit',
             description='Run unit tests',
             category='testing')
    @CommandArgument('test_name', nargs=argparse.REMAINDER,
                     help="Only run tests that match this pattern or file path")
    @CommandArgument('--package', '-p', default=None, help="Specific package to test")
    @CommandArgument('--bench', default=False, action="store_true",
                     help="Run in bench mode")
    @CommandArgument('--nocapture', default=False, action="store_true",
                     help="Run tests with nocapture ( show test stdout )")
    @CommandBase.build_like_command_arguments
    def test_unit(self, test_name=None, package=None, bench=False, nocapture=False, with_layout_2020=False, **kwargs):
        if test_name is None:
            test_name = []

        self.ensure_bootstrapped()

        if package:
            packages = {package}
        else:
            packages = set()

        test_patterns = []
        for test in test_name:
            # add package if 'tests/unit/<package>'
            match = re.search("tests/unit/(\\w+)/?$", test)
            if match:
                packages.add(match.group(1))
            # add package & test if '<package>/<test>', 'tests/unit/<package>/<test>.rs', or similar
            elif re.search("\\w/\\w", test):
                tokens = test.split("/")
                packages.add(tokens[-2])
                test_prefix = tokens[-1]
                if test_prefix.endswith(".rs"):
                    test_prefix = test_prefix[:-3]
                test_prefix += "::"
                test_patterns.append(test_prefix)
            # add test as-is otherwise
            else:
                test_patterns.append(test)

        self_contained_tests = [
            "background_hang_monitor",
            "gfx",
            "msg",
            "net",
            "net_traits",
            "selectors",
            "script_traits",
            "servo_config",
            "servo_remutex",
        ]
        if with_layout_2020:
            self_contained_tests.append("layout_2020")
        else:
            self_contained_tests.append("layout_2013")
        if not packages:
            packages = set(os.listdir(path.join(self.context.topdir, "tests", "unit"))) - set(['.DS_Store'])
            packages |= set(self_contained_tests)

        in_crate_packages = []
        for crate in self_contained_tests:
            try:
                packages.remove(crate)
                in_crate_packages += [crate]
            except KeyError:
                pass

        packages.discard('stylo')

        if len(packages) > 0 or len(in_crate_packages) > 0:
            args = []
            for crate in packages:
                args += ["-p", "%s_tests" % crate]
            for crate in in_crate_packages:
                args += ["-p", crate]
            args += test_patterns

            if nocapture:
                args += ["--", "--nocapture"]

            err = self.run_cargo_build_like_command("bench" if bench else "test",
                                                    args,
                                                    env=self.build_env(test_unit=True),
                                                    with_layout_2020=with_layout_2020,
                                                    **kwargs)
            if err:
                return err

    @Command('test-content',
             description='Run the content tests',
             category='testing')
    def test_content(self):
        print("Content tests have been replaced by web-platform-tests under "
              "tests/wpt/mozilla/.")
        return 0

    def install_rustfmt(self):
        self.ensure_bootstrapped()
        with open(os.devnull, "w") as devnull:
            if self.call_rustup_run(["cargo", "fmt", "--version", "-q"],
                                    stderr=devnull) != 0:
                # Rustfmt is not installed. Install:
                self.call_rustup_run(["rustup", "component", "add", "rustfmt-preview"])

    @Command('test-tidy',
             description='Run the source code tidiness check',
             category='testing')
    @CommandArgument('--all', default=False, action="store_true", dest="all_files",
                     help="Check all files, and run the WPT lint in tidy, "
                          "even if unchanged")
    @CommandArgument('--no-wpt', default=False, action="store_true", dest="no_wpt",
                     help="Skip checking that web-platform-tests manifests are up to date")
    @CommandArgument('--no-progress', default=False, action="store_true",
                     help="Don't show progress for tidy")
    @CommandArgument('--self-test', default=False, action="store_true",
                     help="Run unit tests for tidy")
    @CommandArgument('--stylo', default=False, action="store_true",
                     help="Only handle files in the stylo tree")
    @CommandArgument('--force-cpp', default=False, action="store_true", help="Force CPP check")
    def test_tidy(self, all_files, no_progress, self_test, stylo, force_cpp=False, no_wpt=False):
        if self_test:
            return test_tidy.do_tests()
        else:
            if no_wpt:
                manifest_dirty = False
            else:
                manifest_dirty = wpt.manifestupdate.update(check_clean=True)
            tidy_failed = tidy.scan(not all_files, not no_progress, stylo=stylo, no_wpt=no_wpt)
            self.install_rustfmt()
            rustfmt_failed = self.call_rustup_run(["cargo", "fmt", "--", "--check"])

            env = self.build_env()
            clangfmt_failed = False
            available, cmd, files = setup_clangfmt(env)
            if available:
                for file in files:
                    stdout = check_output([cmd, "-output-replacements-xml", file], env=env)
                    if len(XML(stdout)) > 0:
                        print("%s is not formatted correctly." % file)
                        clangfmt_failed = True
            elif force_cpp:
                print("Error: can't find suitable clang-format version. Required with --force-cpp.")
                return True

            if rustfmt_failed or clangfmt_failed:
                print("Run `./mach fmt` to fix the formatting")

            return tidy_failed or manifest_dirty or rustfmt_failed or clangfmt_failed

    @Command('test-webidl',
             description='Run the WebIDL parser tests',
             category='testing')
    @CommandArgument('--quiet', '-q', default=False, action="store_true",
                     help="Don't print passing tests.")
    @CommandArgument('tests', default=None, nargs="...",
                     help="Specific tests to run, relative to the tests directory")
    def test_webidl(self, quiet, tests):
        test_file_dir = path.abspath(path.join(PROJECT_TOPLEVEL_PATH, "components", "script",
                                               "dom", "bindings", "codegen", "parser"))
        # For the `import WebIDL` in runtests.py
        sys.path.insert(0, test_file_dir)

        run_file = path.abspath(path.join(test_file_dir, "runtests.py"))
        run_globals = {"__file__": run_file}
        exec(compile(open(run_file).read(), run_file, 'exec'), run_globals)

        verbose = not quiet
        return run_globals["run_tests"](tests, verbose)

    @Command('test-wpt-failure',
             description='Run the tests harness that verifies that the test failures are reported correctly',
             category='testing',
             parser=wpt.create_parser)
    def test_wpt_failure(self, **kwargs):
        kwargs["pause_after_test"] = False
        kwargs["include"] = ["infrastructure/failing-test.html"]
        return not self._test_wpt(**kwargs)

    @Command('test-wpt',
             description='Run the regular web platform test suite',
             category='testing',
             parser=wpt.create_parser)
    def test_wpt(self, **kwargs):
        ret = self.run_test_list_or_dispatch(kwargs["test_list"], "wpt", self._test_wpt, **kwargs)
        if kwargs["always_succeed"]:
            return 0
        else:
            return ret

    @Command('test-wpt-android',
             description='Run the web platform test suite in an Android emulator',
             category='testing',
             parser=wpt.create_parser)
    def test_wpt_android(self, release=False, dev=False, binary_args=None, **kwargs):
        kwargs.update(
            release=release,
            dev=dev,
            product="servodriver",
            processes=1,
            binary_args=self.in_android_emulator(release, dev) + (binary_args or []),
            binary=sys.executable,
        )
        return self._test_wpt(android=True, **kwargs)

    def _test_wpt(self, android=False, **kwargs):
        if not android:
            os.environ.update(self.build_env())
        return wpt.run.run_tests(**kwargs)

    # Helper to ensure all specified paths are handled, otherwise dispatch to appropriate test suite.
    def run_test_list_or_dispatch(self, requested_paths, correct_suite, correct_function, **kwargs):
        if not requested_paths:
            return correct_function(**kwargs)
        # Paths specified on command line. Ensure they can be handled, re-dispatch otherwise.
        all_handled = True
        for test_path in requested_paths:
            suite = self.suite_for_path(test_path)
            if suite is not None and correct_suite != suite:
                all_handled = False
                print("Warning: %s is not a %s test. Delegating to test-%s." % (test_path, correct_suite, suite))
        if all_handled:
            return correct_function(**kwargs)
        # Dispatch each test to the correct suite via test()
        Registrar.dispatch("test", context=self.context, params=requested_paths)

    @Command('update-manifest',
             description='Run test-wpt --manifest-update SKIP_TESTS to regenerate MANIFEST.json',
             category='testing',
             parser=wpt.manifestupdate.create_parser)
    def update_manifest(self, **kwargs):
        return wpt.manifestupdate.update(check_clean=False)

    @Command('fmt',
             description='Format the Rust and CPP source files with rustfmt and clang-format',
             category='testing')
    def format_code(self):

        env = self.build_env()
        available, cmd, files = setup_clangfmt(env)
        if available and len(files) > 0:
            check_call([cmd, "-i"] + files, env=env)

        self.install_rustfmt()
        return self.call_rustup_run(["cargo", "fmt"])

    @Command('update-wpt',
             description='Update the web platform tests',
             category='testing',
             parser=wpt.update.create_parser)
    def update_wpt(self, **kwargs):
        patch = kwargs.get("patch", False)
        if not patch and kwargs["sync"]:
            print("Are you sure you don't want a patch?")
            return 1
        return wpt.update.update_tests(**kwargs)

    @Command('test-android-startup',
             description='Extremely minimal testing of Servo for Android',
             category='testing')
    @CommandArgument('--release', '-r', action='store_true',
                     help='Run the release build')
    @CommandArgument('--dev', '-d', action='store_true',
                     help='Run the dev build')
    def test_android_startup(self, release, dev):
        html = """
            <script>
                window.alert("JavaScript is running!")
            </script>
        """
        url = "data:text/html;base64," + html.encode("base64").replace("\n", "")
        args = self.in_android_emulator(release, dev)
        args = [sys.executable] + args + [url]
        process = subprocess.Popen(args, stdout=subprocess.PIPE)
        try:
            while 1:
                line = process.stdout.readline()
                if len(line) == 0:
                    print("EOF without finding the expected line")
                    return 1
                print(line.rstrip())
                if "JavaScript is running!" in line:
                    break
        finally:
            process.terminate()

    def in_android_emulator(self, release, dev):
        if (release and dev) or not (release or dev):
            print("Please specify one of --dev or --release.")
            sys.exit(1)

        avd = "servo-x86"
        target = "i686-linux-android"
        print("Assuming --target " + target)

        env = self.build_env(target=target)
        os.environ["PATH"] = env["PATH"]
        assert self.handle_android_target(target)
        apk = self.get_apk_path(release)

        py = path.join(self.context.topdir, "etc", "run_in_headless_android_emulator.py")
        return [py, avd, apk]

    @Command('test-jquery',
             description='Run the jQuery test suite',
             category='testing')
    @CommandArgument('--release', '-r', action='store_true',
                     help='Run the release build')
    @CommandArgument('--dev', '-d', action='store_true',
                     help='Run the dev build')
    def test_jquery(self, release, dev):
        return self.jquery_test_runner("test", release, dev)

    @Command('test-dromaeo',
             description='Run the Dromaeo test suite',
             category='testing')
    @CommandArgument('tests', default=["recommended"], nargs="...",
                     help="Specific tests to run")
    @CommandArgument('--release', '-r', action='store_true',
                     help='Run the release build')
    @CommandArgument('--dev', '-d', action='store_true',
                     help='Run the dev build')
    def test_dromaeo(self, tests, release, dev):
        return self.dromaeo_test_runner(tests, release, dev)

    @Command('update-jquery',
             description='Update the jQuery test suite expected results',
             category='testing')
    @CommandArgument('--release', '-r', action='store_true',
                     help='Run the release build')
    @CommandArgument('--dev', '-d', action='store_true',
                     help='Run the dev build')
    def update_jquery(self, release, dev):
        return self.jquery_test_runner("update", release, dev)

    @Command('compare_dromaeo',
             description='Compare outputs of two runs of ./mach test-dromaeo command',
             category='testing')
    @CommandArgument('params', default=None, nargs="...",
                     help=" filepaths of output files of two runs of dromaeo test ")
    def compare_dromaeo(self, params):
        prev_op_filename = params[0]
        cur_op_filename = params[1]
        result = {'Test': [], 'Prev_Time': [], 'Cur_Time': [], 'Difference(%)': []}
        with open(prev_op_filename, 'r') as prev_op, open(cur_op_filename, 'r') as cur_op:
            l1 = prev_op.readline()
            l2 = cur_op.readline()

            while ((l1.find('[dromaeo] Saving...') and l2.find('[dromaeo] Saving...'))):
                l1 = prev_op.readline()
                l2 = cur_op.readline()

            reach = 3
            while (reach > 0):
                l1 = prev_op.readline()
                l2 = cur_op.readline()
                reach -= 1

            while True:
                l1 = prev_op.readline()
                l2 = cur_op.readline()
                if not l1:
                    break
                result['Test'].append(str(l1).split('|')[0].strip())
                result['Prev_Time'].append(float(str(l1).split('|')[1].strip()))
                result['Cur_Time'].append(float(str(l2).split('|')[1].strip()))
                a = float(str(l1).split('|')[1].strip())
                b = float(str(l2).split('|')[1].strip())
                result['Difference(%)'].append(((b - a) / a) * 100)

            width_col1 = max([len(x) for x in result['Test']])
            width_col2 = max([len(str(x)) for x in result['Prev_Time']])
            width_col3 = max([len(str(x)) for x in result['Cur_Time']])
            width_col4 = max([len(str(x)) for x in result['Difference(%)']])

            for p, q, r, s in zip(['Test'], ['First Run'], ['Second Run'], ['Difference(%)']):
                print("\033[1m" + "{}|{}|{}|{}".format(p.ljust(width_col1), q.ljust(width_col2), r.ljust(width_col3),
                      s.ljust(width_col4)) + "\033[0m" + "\n" + "--------------------------------------------------"
                      + "-------------------------------------------------------------------------")

            for a1, b1, c1, d1 in zip(result['Test'], result['Prev_Time'], result['Cur_Time'], result['Difference(%)']):
                if d1 > 0:
                    print("\033[91m" + "{}|{}|{}|{}".format(a1.ljust(width_col1),
                          str(b1).ljust(width_col2), str(c1).ljust(width_col3), str(d1).ljust(width_col4)) + "\033[0m")
                elif d1 < 0:
                    print("\033[92m" + "{}|{}|{}|{}".format(a1.ljust(width_col1),
                          str(b1).ljust(width_col2), str(c1).ljust(width_col3), str(d1).ljust(width_col4)) + "\033[0m")
                else:
                    print("{}|{}|{}|{}".format(a1.ljust(width_col1), str(b1).ljust(width_col2),
                          str(c1).ljust(width_col3), str(d1).ljust(width_col4)))

    def jquery_test_runner(self, cmd, release, dev):
        base_dir = path.abspath(path.join("tests", "jquery"))
        jquery_dir = path.join(base_dir, "jquery")
        run_file = path.join(base_dir, "run_jquery.py")

        # Clone the jQuery repository if it doesn't exist
        if not os.path.isdir(jquery_dir):
            check_call(
                ["git", "clone", "-b", "servo", "--depth", "1", "https://github.com/servo/jquery", jquery_dir])

        # Run pull in case the jQuery repo was updated since last test run
        check_call(
            ["git", "-C", jquery_dir, "pull"])

        # Check that a release servo build exists
        bin_path = path.abspath(self.get_binary_path(release, dev))

        return call([run_file, cmd, bin_path, base_dir])

    def dromaeo_test_runner(self, tests, release, dev):
        base_dir = path.abspath(path.join("tests", "dromaeo"))
        dromaeo_dir = path.join(base_dir, "dromaeo")
        run_file = path.join(base_dir, "run_dromaeo.py")

        # Clone the Dromaeo repository if it doesn't exist
        if not os.path.isdir(dromaeo_dir):
            check_call(
                ["git", "clone", "-b", "servo", "--depth", "1", "https://github.com/notriddle/dromaeo", dromaeo_dir])

        # Run pull in case the Dromaeo repo was updated since last test run
        check_call(
            ["git", "-C", dromaeo_dir, "pull"])

        # Compile test suite
        check_call(
            ["make", "-C", dromaeo_dir, "web"])

        # Check that a release servo build exists
        bin_path = path.abspath(self.get_binary_path(release, dev))

        return check_call(
            [run_file, "|".join(tests), bin_path, base_dir])


def setup_clangfmt(env):
    cmd = "clang-format.exe" if sys.platform == "win32" else "clang-format"
    try:
        version = check_output([cmd, "--version"], env=env, universal_newlines=True).rstrip()
        print(version)
        if version.find("clang-format version {}.".format(CLANGFMT_VERSION)) == -1:
            print("clang-format: wrong version (v{} required). Skipping CPP formatting.".format(CLANGFMT_VERSION))
            return False, None, None
    except OSError:
        print("clang-format not installed. Skipping CPP formatting.")
        return False, None, None
    gitcmd = ['git', 'ls-files']
    gitfiles = check_output(gitcmd + CLANGFMT_CPP_DIRS, universal_newlines=True).splitlines()
    filtered = [line for line in gitfiles if line.endswith(".h") or line.endswith(".cpp")]
    return True, cmd, filtered


def create_parser_create():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--no-editor", action="store_true",
                   help="Don't try to open the test in an editor")
    p.add_argument("-e", "--editor", action="store", help="Editor to use")
    p.add_argument("--no-run", action="store_true",
                   help="Don't try to update the wpt manifest or open the test in a browser")
    p.add_argument('--release', action="store_true",
                   help="Run with a release build of servo")
    p.add_argument("--long-timeout", action="store_true",
                   help="Test should be given a long timeout (typically 60s rather than 10s,"
                   "but varies depending on environment)")
    p.add_argument("--overwrite", action="store_true",
                   help="Allow overwriting an existing test file")
    p.add_argument("-r", "--reftest", action="store_true",
                   help="Create a reftest rather than a testharness (js) test"),
    p.add_argument("-ref", "--reference", dest="ref", help="Path to the reference file")
    p.add_argument("--mismatch", action="store_true",
                   help="Create a mismatch reftest")
    p.add_argument("--wait", action="store_true",
                   help="Create a reftest that waits until takeScreenshot() is called")
    p.add_argument("path", action="store", help="Path to the test file")
    return p


@CommandProvider
class WebPlatformTestsCreator(CommandBase):
    template_prefix = """<!doctype html>
%(documentElement)s<meta charset="utf-8">
"""
    template_long_timeout = "<meta name=timeout content=long>\n"

    template_body_th = """<title></title>
<script src="/resources/testharness.js"></script>
<script src="/resources/testharnessreport.js"></script>
<script>

</script>
"""

    template_body_reftest = """<title></title>
<link rel="%(match)s" href="%(ref)s">
"""

    template_body_reftest_wait = """<script src="/common/reftest-wait.js"></script>
"""

    def make_test_file_url(self, absolute_file_path):
        # Make the path relative to the project top-level directory so that
        # we can more easily find the right test directory.
        file_path = os.path.relpath(absolute_file_path, PROJECT_TOPLEVEL_PATH)

        if file_path.startswith(WEB_PLATFORM_TESTS_PATH):
            url = file_path[len(WEB_PLATFORM_TESTS_PATH):]
        elif file_path.startswith(SERVO_TESTS_PATH):
            url = "/mozilla" + file_path[len(SERVO_TESTS_PATH):]
        else:  # This test file isn't in any known test directory.
            return None

        return url.replace(os.path.sep, "/")

    def make_test_and_reference_urls(self, test_path, reference_path):
        test_path = os.path.normpath(os.path.abspath(test_path))
        test_url = self.make_test_file_url(test_path)
        if test_url is None:
            return (None, None)

        if reference_path is None:
            return (test_url, '')
        reference_path = os.path.normpath(os.path.abspath(reference_path))

        # If the reference is in the same directory, the URL can just be the
        # name of the refernce file itself.
        reference_path_parts = os.path.split(reference_path)
        if reference_path_parts[0] == os.path.split(test_path)[0]:
            return (test_url, reference_path_parts[1])
        return (test_url, self.make_test_file_url(reference_path))

    @Command("create-wpt",
             category="testing",
             parser=create_parser_create)
    def run_create(self, **kwargs):
        import subprocess

        test_path = kwargs["path"]
        reference_path = kwargs["ref"]

        if reference_path:
            kwargs["reftest"] = True

        (test_url, reference_url) = self.make_test_and_reference_urls(
            test_path, reference_path)

        if test_url is None:
            print("""Test path %s is not in wpt directories:
tests/wpt/web-platform-tests for tests that may be shared
tests/wpt/mozilla/tests for Servo-only tests""" % test_path)
            return 1

        if reference_url is None:
            print("""Reference path %s is not in wpt directories:
testing/web-platform/tests for tests that may be shared
testing/web-platform/mozilla/tests for Servo-only tests""" % reference_path)
            return 1

        if os.path.exists(test_path) and not kwargs["overwrite"]:
            print("Test path already exists, pass --overwrite to replace")
            return 1

        if kwargs["mismatch"] and not kwargs["reftest"]:
            print("--mismatch only makes sense for a reftest")
            return 1

        if kwargs["wait"] and not kwargs["reftest"]:
            print("--wait only makes sense for a reftest")
            return 1

        args = {"documentElement": "<html class=\"reftest-wait\">\n" if kwargs["wait"] else ""}
        template = self.template_prefix % args
        if kwargs["long_timeout"]:
            template += self.template_long_timeout

        if kwargs["reftest"]:
            args = {"match": "match" if not kwargs["mismatch"] else "mismatch",
                    "ref": reference_url}
            template += self.template_body_reftest % args
            if kwargs["wait"]:
                template += self.template_body_reftest_wait
        else:
            template += self.template_body_th
        with open(test_path, "w") as f:
            f.write(template)

        if kwargs["no_editor"]:
            editor = None
        elif kwargs["editor"]:
            editor = kwargs["editor"]
        elif "VISUAL" in os.environ:
            editor = os.environ["VISUAL"]
        elif "EDITOR" in os.environ:
            editor = os.environ["EDITOR"]
        else:
            editor = None

        if editor:
            proc = subprocess.Popen("%s %s" % (editor, test_path), shell=True)

        if not kwargs["no_run"]:
            p = wpt.create_parser()
            args = []
            if kwargs["release"]:
                args.append("--release")
            args.append(test_path)
            wpt_kwargs = vars(p.parse_args(args))
            self.context.commands.dispatch("test-wpt", self.context, **wpt_kwargs)
            self.context.commands.dispatch("update-manifest", self.context)

        if editor:
            proc.wait()

    @Command('update-net-cookies',
             description='Update the net unit tests with cookie tests from http-state',
             category='testing')
    def update_net_cookies(self):
        cache_dir = path.join(self.config["tools"]["cache-dir"], "tests")
        run_file = path.abspath(path.join(PROJECT_TOPLEVEL_PATH,
                                          "components", "net", "tests",
                                          "cookie_http_state_utils.py"))
        run_globals = {"__file__": run_file}
        exec(compile(open(run_file).read(), run_file, 'exec'), run_globals)
        return run_globals["update_test_file"](cache_dir)

    @Command('update-webgl',
             description='Update the WebGL conformance suite tests from Khronos repo',
             category='testing')
    @CommandArgument('--version', default='2.0.0',
                     help='WebGL conformance suite version')
    def update_webgl(self, version=None):
        base_dir = path.abspath(path.join(PROJECT_TOPLEVEL_PATH,
                                "tests", "wpt", "mozilla", "tests", "webgl"))
        run_file = path.join(base_dir, "tools", "import-conformance-tests.py")
        dest_folder = path.join(base_dir, "conformance-%s" % version)
        patches_dir = path.join(base_dir, "tools")
        # Clean dest folder if exists
        if os.path.exists(dest_folder):
            shutil.rmtree(dest_folder)

        run_globals = {"__file__": run_file}
        exec(compile(open(run_file).read(), run_file, 'exec'), run_globals)
        return run_globals["update_conformance"](version, dest_folder, None, patches_dir)

    @Command('smoketest',
             description='Load a simple page in Servo and ensure that it closes properly',
             category='testing')
    @CommandArgument('params', nargs='...',
                     help="Command-line arguments to be passed through to Servo")
    def smoketest(self, params):
        # We pass `-f` here so that any thread panic will cause Servo to exit,
        # preventing a panic from hanging execution. This means that these kind
        # of panics won't cause timeouts on CI.
        params = params + ['-f', 'tests/html/close-on-load.html']
        return self.context.commands.dispatch(
            'run', self.context, params=params)
