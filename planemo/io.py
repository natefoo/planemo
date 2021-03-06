from __future__ import print_function
from __future__ import absolute_import

import contextlib
import os
import shutil
import sys
import tempfile
import time
from six import StringIO
from xml.sax.saxutils import escape

import click
from galaxy.tools.deps import commands
from galaxy.tools.deps.commands import download_command
from .exit_codes import (
    EXIT_CODE_OK,
    EXIT_CODE_NO_SUCH_TARGET,
)


def communicate(cmds, **kwds):
    if isinstance(cmds, list):
        cmds = commands.argv_to_str(cmds)
    info(cmds)
    p = commands.shell_process(cmds, **kwds)
    if kwds.get("stdout", None) is None and commands.redirecting_io(sys=sys):
        output = commands.redirect_aware_commmunicate(p)
    else:
        output = p.communicate()

    if p.returncode != 0:
        template = "Problem executing commands {0} - ({1}, {2})"
        msg = template.format(cmds, output[0], output[1])
        raise RuntimeError(msg)
    return output


def shell(cmds, **kwds):
    info(cmds)
    return commands.shell(cmds, **kwds)


def info(message, *args):
    if args:
        message = message % args
    _echo(click.style(message, bold=True, fg='green'))


def can_write_to_path(path, **kwds):
    if not kwds["force"] and os.path.exists(path):
        error("%s already exists, exiting." % path)
        return False
    return True


def error(message, *args):
    if args:
        message = message % args
    _echo(click.style(message, bold=True, fg='red'), err=True)


def warn(message, *args):
    if args:
        message = message % args
    _echo(click.style(message, fg='red'), err=True)


def _echo(message, err=False):
    if sys.version_info[0] == 2:
        click.echo(message, err=err)
    else:
        print(message)


def shell_join(*args):
    """Join potentially empty commands together with ;."""
    return "; ".join([c for c in args if c])


def write_file(path, content, force=True):
    if os.path.exists(path) and not force:
        return

    with open(path, "w") as f:
        f.write(content)


def untar_to(url, path, tar_args):
    download_cmd = " ".join(download_command(url, quote_url=True))
    if tar_args:
        if not os.path.exists(path):
            os.makedirs(path)

        untar_cmd = "tar %s" % tar_args
        shell("%s | %s" % (download_cmd, untar_cmd))
    else:
        shell("%s > '%s'" % (download_cmd, path))


@contextlib.contextmanager
def real_io():
    """Ensure stdout and stderr have ``fileno`` attributes.

    nosetests replaces these streams with :class:`StringIO` objects
    that may not work the same in every situtation - :func:`subprocess.Popen`
    calls in particular.
    """
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    try:
        if not hasattr(sys.stdout, "fileno"):
            sys.stdout = sys.__stdout__
        if not hasattr(sys.stderr, "fileno"):
            sys.stderr = sys.__stderr__
        yield
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr


@contextlib.contextmanager
def temp_directory(prefix="planemo_tmp_"):
    temp_dir = tempfile.mkdtemp(prefix=prefix)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


def ps1_for_path(path, base="PS1"):
    """ Used by environment commands to build a PS1 shell
    variables for tool or directory of tools.
    """
    file_name = os.path.basename(path)
    base_name = os.path.splitext(file_name)[0]
    ps1 = "(%s)${%s}" % (base_name, base)
    return ps1


def kill_pid_file(pid_file):
    if not os.path.exists(pid_file):
        return

    pid = int(open(pid_file, "r").read())
    kill_posix(pid)


def kill_posix(pid):
    def _check_pid():
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    if _check_pid():
        for sig in [15, 9]:
            try:
                os.kill(pid, sig)
            except OSError:
                return
            time.sleep(1)
            if not _check_pid():
                return


@contextlib.contextmanager
def conditionally_captured_io(capture, tee=False):
    captured_std = []
    if capture:
        with Capturing() as captured_std:
            yield captured_std
        if tee:
            tee_captured_output(captured_std)
    else:
        yield


@contextlib.contextmanager
def captured_io_for_xunit(kwds, captured_io):
    captured_std = []
    with_xunit = kwds.get('report_xunit', False)
    with conditionally_captured_io(with_xunit, tee=True):
        time1 = time.time()
        yield
        time2 = time.time()

    if with_xunit:
        stdout = [escape(m['data']) for m in captured_std
                  if m['logger'] == 'stdout']
        stderr = [escape(m['data']) for m in captured_std
                  if m['logger'] == 'stderr']
        captured_io["stdout"] = stdout
        captured_io["stderr"] = stderr
        captured_io["time"] = (time2 - time1)
    else:
        captured_io["stdout"] = None
        captured_io["stderr"] = None
        captured_io["time"] = None


class Capturing(list):
    """Function context which captures stdout/stderr

    This keeps planemo's codebase clean without requiring planemo to hold onto
    messages, or pass user-facing messages back at all. This could probably be
    solved by swapping planemo entirely to a logger and reading from/writing
    to that, but this is easier.

    This swaps sys.std{out,err} with StringIOs and then makes that output
    available.
    """
    # http://stackoverflow.com/a/16571630

    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = self._stringio_stdout = StringIO()
        sys.stderr = self._stringio_stderr = StringIO()
        return self

    def __exit__(self, *args):
        self.extend([{'logger': 'stdout', 'data': x} for x in
                     self._stringio_stdout.getvalue().splitlines()])
        self.extend([{'logger': 'stderr', 'data': x} for x in
                     self._stringio_stderr.getvalue().splitlines()])

        sys.stdout = self._stdout
        sys.stderr = self._stderr


def tee_captured_output(output):
    """For messages captured with Capturing, send them to their correct
    locations so as to not interfere with normal user experience.
    """
    for message in output:
        # Append '\n' due to `splitlines()` above
        if message['logger'] == 'stdout':
            sys.stdout.write(message['data'] + '\n')
        if message['logger'] == 'stderr':
            sys.stderr.write(message['data'] + '\n')


# Taken from Galaxy's twilltestcase.
def wait_on(function, desc, timeout=5):
    delta = .25
    iteration = 0
    while True:
        if (delta * iteration) > timeout:
            message = "Timed out waiting on %s." % desc
            raise Exception(message)

        iteration += 1
        value = function()
        if value is not None:
            return value

        time.sleep(delta)


def coalesce_return_codes(ret_codes, assert_at_least_one=False):
    # Return 0 if everything is fine, otherwise pick the least
    # specific non-0 return code - preferring to report errors
    # to other non-0 exit codes.
    if assert_at_least_one and len(ret_codes) == 0:
        return EXIT_CODE_NO_SUCH_TARGET

    coalesced_return_code = EXIT_CODE_OK
    for ret_code in ret_codes:
        # None is equivalent to 0 in these methods.
        ret_code = 0 if ret_code is None else ret_code
        if ret_code == 0:
            # Everything is fine, keep moving...
            pass
        elif coalesced_return_code == 0:
            coalesced_return_code = ret_code
        # At this point in logic both ret_code and coalesced_return_code are
        # are non-zero
        elif ret_code < 0:
            # Error state, this should override eveything else.
            coalesced_return_code = ret_code
        elif ret_code > 0 and coalesced_return_code < 0:
            # Keep error state recorded.
            pass
        elif ret_code > 0:
            # Lets somewhat arbitrarily call the smaller exit code
            # the less specific.
            coalesced_return_code = min(ret_code, coalesced_return_code)

    if coalesced_return_code < 0:
        # Map -1 => 254, -2 => 253, etc...
        # Not sure it is helpful to have negative error codes
        # this was a design and API mistake in planemo.
        coalesced_return_code = 255 + coalesced_return_code

    return coalesced_return_code
