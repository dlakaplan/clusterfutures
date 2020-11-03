"""Python futures for Condor clusters."""
from concurrent import futures
import os
import sys
import threading
import time
from . import condor
from . import slurm
from .remote import INFILE_FMT, OUTFILE_FMT
from .util import random_string, local_filename
import cloudpickle

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

    def _start(workerid):
        """Start a job with the given worker ID and return an ID
        identifying the new job. The job should run ``python -m
        cfut.remote <workerid>.
        """
        raise NotImplementedError()

    def _cleanup(jobid):
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
            print("job completed: %s" % str(jobid), file=sys.stdout)

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

    def submit(self, fun, *args, additional_setup_lines=[], **kwargs):
        """Submit a job to the pool."""
        fut = futures.Future()

        # Start the job.
        workerid = random_string()
        funcser = cloudpickle.dumps((fun, args, kwargs))
        with open(INFILE_FMT % workerid, 'wb') as f:
            f.write(funcser)
        jobid = self._start(workerid, additional_setup_lines)

        if self.debug:
            print("job submitted: %s" % str(jobid), file=sys.stdout)

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
    """Futures executor for executing jobs on a Slurm cluster."""
    def _start(self, workerid, additional_setup_lines):
        return slurm.submit(
            '{} -m cfut.remote {}'.format(sys.executable, workerid), additional_setup_lines=additional_setup_lines)

    def _start_array(self, workerid_base, additional_setup_lines):
        return slurm.submit_array(
            '{} -m cfut.remote {}_$SLURM_ARRAY_TASK_ID'.format(sys.executable, workerid_base), additional_setup_lines=additional_setup_lines)

    def _cleanup(self, jobid):
        if self.keep_logs:
            return

        outf = slurm.OUTFILE_FMT.format(str(jobid))
        try:
            os.unlink(outf)
        except OSError:
            pass

    def submit_array(self, fun, args, additional_setup_lines=[],kwargs=[],batch_size=1):
        """Submit a job to the pool. args should be single iterable,
        kwargs still needs to be tested, input as list of mappings for now"""
        if len(kwargs) == 0:
            kwargs = [{} for _ in args] # empty dicts
        elif len(kwargs) != len(args):
            raise NotImplementedError('Number of kwarg dicts must equal number of arg lists')
        # Start the job array.
        workerids = []
        workerid_base = random_string()
        if len(args) % batch_size != 0: # TODO: make compatible with number of args non-divisible by batch size (some workers do additional work)
            raise NotImplementedError('Number of arguments must be divisible by batch size')

        num_jobs = int(len(args)/batch_size)
        for i in range(0,num_jobs):
            workerid = workerid_base + '_%d' % i # i will be $SLURM_ARRAY_TASK_ID (starting at 0)
            inds = (batch_size*i,batch_size*(i+1))
            arg = args[inds[0]:inds[1]] # list of args in each batch
            kwarg = kwargs[inds[0]:inds[1]] # list of kwargs in each batch
            funcser = cloudpickle.dumps((fun,arg,kwarg))
            with open(INFILE_FMT % workerid, 'wb') as f:
                f.write(funcser)
            workerids.append(workerid)

        # for i, (arg,kwarg) in enumerate(zip(args,kwargs)):
        #     workerid = workerid_base + '_%d' % i # i will be $SLURM_ARRAY_TASK_ID (starting at 0)
        #     funcser = cloudpickle.dumps((fun, [arg], kwarg))
        #     with open(INFILE_FMT % workerid, 'wb') as f:
        #         f.write(funcser)
        #     workerids.append(workerid)

        # submit job array with length equal to number of arguments,
        # each job in array matches a workerid pickle file (fully parallelized)
        additional_setup_lines.append("#SBATCH --array=0-{}".format(num_jobs-1))
        jobid = self._start_array(workerid_base, additional_setup_lines)
        jobids = ['%d_%d' % (jobid,i) for i in range(0,num_jobs)] # note: using string, rather than int
        if self.debug:
            print("job array submitted: %d_0-%d" % (jobid,num_jobs-1), file=sys.stderr)

        # Thread will wait for all jobs to finish.
        futs = []
        for workerid,jobid in zip(workerids,jobids):
            fut = futures.Future()
            self.wait_thread.wait(OUTFILE_FMT % workerid, jobid)

            with self.jobs_lock:
                self.jobs[jobid] = (fut, workerid)
            futs.append(fut)
        return futs

class CondorExecutor(ClusterExecutor):
    """Futures executor for executing jobs on a Condor cluster."""
    def __init__(self, debug=False):
        super(CondorExecutor, self).__init__(debug)
        self.logfile = LOGFILE_FMT % random_string()

    def _start(self, workerid, additional_setup_lines):
        return condor.submit(sys.executable, '-m cfut.remote %s' % workerid,
                             log=self.logfile)

    def _cleanup(self, jobid):
        os.unlink(condor.OUTFILE_FMT % str(jobid))
        os.unlink(condor.ERRFILE_FMT % str(jobid))

    def shutdown(self, wait=True):
        super(CondorExecutor, self).shutdown(wait)
        if os.path.exists(self.logfile):
            os.unlink(self.logfile)

def map(executor, func, args, ordered=True,additional_setup_lines=[]):
    """Convenience function to map a function over cluster jobs. Given
    a function and an iterable, generates results. (Works like
    ``itertools.imap``.) If ``ordered`` is False, then the values are
    generated in an undefined order, possibly more quickly.
    """
    with executor:
        futs = []
        for arg in args:
            futs.append(executor.submit(func, arg, additional_setup_lines=additional_setup_lines))
        for fut in (futs if ordered else futures.as_completed(futs)):
            yield fut.result()

def map_array(executor, func, args, ordered=True,additional_setup_lines=[],batch_size=1):
    """Convenience function to map a function over cluster job arrays (--array).
    Given a function and an iterable, generates results. (Works like
    ``itertools.imap``.) If ``ordered`` is False, then the values are
    generated in an undefined order, possibly more quickly.
    """
    results = [] # if batch_size > 1
    with executor:
        futs = executor.submit_array(func, args, additional_setup_lines,batch_size=batch_size)
        for fut in (futs if ordered else futures.as_completed(futs)):
            if batch_size == 1:
                yield fut.result()
            else:
                # yield zip(*fut.result())
                results.append(list(fut.result()))

        if batch_size > 1:
            from itertools import chain
            # yield from zip(*results)
            yield from chain(*results)