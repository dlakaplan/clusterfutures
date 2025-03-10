"""Python futures for Condor clusters."""
from concurrent import futures
import os
import sys
import threading
import time
from . import condor
from . import slurm
from .util import (
    random_string, local_filename, INFILE_FMT, OUTFILE_FMT,
)
import cloudpickle

__version__ = '0.4'

LOGFILE_FMT = local_filename('cfut.log.%s.txt')

class RemoteException(Exception):
    def __init__(self, error):
        self.error = error

    def __str__(self):
        return '\n' + self.error.strip()

class FileWaitThread(threading.Thread):
    """A thread that polls the filesystem waiting for a list of files to
    be created. When a specified file is created, it invokes a callback.
    """
    def __init__(self, callback, interval=1):
        """The callable ``callback`` will be invoked with value
        associated with the filename of each file that is created.
        ``interval`` specifies the polling rate.
        """
        threading.Thread.__init__(self)
        self.callback = callback
        self.interval = interval
        self.waiting = {}
        self.lock = threading.Lock()
        self.shutdown = False

    def stop(self):
        """Stop the thread soon."""
        with self.lock:
            self.shutdown = True

    def wait(self, filename, value):
        """Adds a new filename (and its associated callback value) to
        the set of files being waited upon.
        """
        with self.lock:
            self.waiting[filename] = value

    def run(self):
        while True:
            with self.lock:
                if self.shutdown:
                    return

                # Poll for each file.
                for filename in list(self.waiting):
                    if os.path.exists(filename):
                        self.callback(self.waiting[filename])
                        del self.waiting[filename]

            time.sleep(self.interval)

class ClusterExecutor(futures.Executor):
    """An abstract base class for executors that run jobs on clusters.
    """
    def __init__(self, debug=False, keep_logs=False):
        os.makedirs(local_filename(), exist_ok=True)
        self.debug = debug

        self.jobs = {}
        self.job_outfiles = {}
        self.jobs_lock = threading.Lock()
        self.jobs_empty_cond = threading.Condition(self.jobs_lock)
        self.keep_logs = keep_logs

        self.wait_thread = FileWaitThread(self._completion)
        self.wait_thread.start()

    def _start(self, workerid, additional_setup_lines):
        """Start a job with the given worker ID and return an ID
        identifying the new job. The job should run ``python -m
        cfut.remote <workerid>.
        """
        raise NotImplementedError()

    def _cleanup(self, jobid):
        """Given a job ID as returned by _start, perform any necessary
        cleanup after the job has finished.
        """

    def _completion(self, jobid):
        """Called whenever a job finishes."""
        with self.jobs_lock:
            fut, workerid = self.jobs.pop(jobid)
            if not self.jobs:
                self.jobs_empty_cond.notify_all()
        if self.debug:
            print("job completed: %i" % jobid, file=sys.stderr)

        with open(OUTFILE_FMT % workerid, 'rb') as f:
            outdata = f.read()
        success, result = cloudpickle.loads(outdata)

        if success:
            fut.set_result(result)
        else:
            fut.set_exception(RemoteException(result))

        # Clean up communication files.
        os.unlink(INFILE_FMT % workerid)
        os.unlink(OUTFILE_FMT % workerid)

        self._cleanup(jobid)

    def submit(self, fun, *args, additional_setup_lines=None, **kwargs):
        """Submit a job to the pool.

        If additional_setup_lines is passed, it overrides the lines given
        when creating the executor.
        """
        fut = futures.Future()

        # Start the job.
        workerid = random_string()
        funcser = cloudpickle.dumps((fun, args, kwargs))
        with open(INFILE_FMT % workerid, 'wb') as f:
            f.write(funcser)
        jobid = self._start(workerid, additional_setup_lines)

        if self.debug:
            print("job submitted: %i" % jobid, file=sys.stderr)

        # Thread will wait for it to finish.
        self.wait_thread.wait(OUTFILE_FMT % workerid, jobid)

        with self.jobs_lock:
            self.jobs[jobid] = (fut, workerid)
        return fut

    def shutdown(self, wait=True):
        """Close the pool."""
        if wait:
            with self.jobs_lock:
                if self.jobs:
                    self.jobs_empty_cond.wait()

        self.wait_thread.stop()
        self.wait_thread.join()

class SlurmExecutor(ClusterExecutor):
    """Futures executor for executing jobs on a Slurm cluster.

    additional_setup_lines is a list of lines to include in the shell script
    passed to sbatch. They may include sbatch options (starting with
    '#SBATCH') and shell commands, e.g. to set environment variables.
    """
    def __init__(self, debug=False, keep_logs=False, additional_setup_lines=()):
        super().__init__(debug, keep_logs)
        self.additional_setup_lines = additional_setup_lines

    def _start(self, workerid, additional_setup_lines):
        if additional_setup_lines is None:
            additional_setup_lines = self.additional_setup_lines
        return slurm.submit(
            '{} -m cfut.remote {}'.format(sys.executable, workerid),
            additional_setup_lines=additional_setup_lines
        )

    def _cleanup(self, jobid):
        if self.keep_logs:
            return

        outf = slurm.OUTFILE_FMT.format(str(jobid))
        try:
            os.unlink(outf)
        except OSError:
            pass

class CondorExecutor(ClusterExecutor):
    """Futures executor for executing jobs on a Condor cluster."""
    def __init__(self, debug=False, keep_logs=False):
        super(CondorExecutor, self).__init__(debug, keep_logs)
        self.logfile = LOGFILE_FMT % random_string()

    def _start(self, workerid, additional_setup_lines):
        return condor.submit(sys.executable, '-m cfut.remote %s' % workerid,
                             log=self.logfile)

    def _cleanup(self, jobid):
        if self.keep_logs:
            return
        os.unlink(condor.OUTFILE_FMT % str(jobid))
        os.unlink(condor.ERRFILE_FMT % str(jobid))

    def shutdown(self, wait=True):
        super(CondorExecutor, self).shutdown(wait)
        if os.path.exists(self.logfile):
            os.unlink(self.logfile)

def map(executor, func, args, ordered=True):
    """Convenience function to map a function over cluster jobs. Given
    a function and an iterable, generates results. (Works like
    ``itertools.imap``.) If ``ordered`` is False, then the values are
    generated in an undefined order, possibly more quickly.
    """
    with executor:
        futs = []
        for arg in args:
            futs.append(executor.submit(func, arg))
        for fut in (futs if ordered else futures.as_completed(futs)):
            yield fut.result()
