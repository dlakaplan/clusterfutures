"""Python futures for cluster computing."""
import condor
from cloud import serialization
from concurrent import futures
import sys
import random
import string
import os

INFILE_FMT = 'cfut.in.%s.pickle'
OUTFILE_FMT = 'cfut.out.%s.pickle'
LOGFILE_FMT = 'cfut.log.%s.txt'

def random_string(length=32, chars=(string.ascii_letters + string.digits)):
    return ''.join(random.choice(chars) for i in range(length))

class RemoteException(object):
    pass

class CondorExecutor(futures.Executor):
    def __init__(self, debug=False):
        self.debug = debug

        self.logfile = LOGFILE_FMT % random_string()
        self.jobs = {}

        self.wait_thread = condor.WaitThread(self._completion, self.logfile)
        self.wait_thread.start()

    def _completion(self, jobid):
        """Called whenever a job finishes."""
        fut, workerid = self.jobs.pop(jobid)
        if self.debug:
            print >>sys.stderr, "job completed: %i" % jobid

        with open(OUTFILE_FMT % workerid) as f:
            outdata = f.read()
        success, result = serialization.deserialize(outdata)

        if success:
            fut.set_result(result)
        else:
            fut.set_exception(RemoteException(result))

        # Clean up communication files.
        os.unlink(INFILE_FMT % workerid)
        os.unlink(OUTFILE_FMT % workerid)
        # Clean up Condor stream files.
        os.unlink(condor.OUTFILE_FMT % str(jobid))
        os.unlink(condor.ERRFILE_FMT % str(jobid))
    
    def submit(self, fun, *args, **kwargs):
        """Submit a job to the pool."""
        fut = futures.Future()

        # Start the job.
        workerid = random_string()
        funcser = serialization.serialize((fun, args, kwargs), True)
        with open(INFILE_FMT % workerid, 'w') as f:
            f.write(funcser)
        jobid = condor.submit(sys.executable, '-m cfut %s' % workerid,
                              log=self.logfile)

        if self.debug:
            print >>sys.stderr, "job submitted: %i" % jobid

        # Thread will wait for it to finish.
        self.wait_thread.wait(jobid)

        self.jobs[jobid] = (fut, workerid)
        return fut

    def shutdown(self, wait=True):
        """Close the pool."""
        #TODO wait
        self.wait_thread.stop()
        if os.path.exists(self.logfile):
            os.unlink(self.logfile)

def _worker(workerid):
    """Called to execute a job on a Condor host."""
    try:
        with open(INFILE_FMT % workerid) as f:
            indata = f.read()
        fun, args, kwargs = serialization.deserialize(indata)

        result = True, fun(*args, **kwargs)
        out = serialization.serialize(result, True)

    except BaseException, exc:
        result = False, str(exc)
        out = serialization.serialize(result, False)

    with open(OUTFILE_FMT % workerid, 'w') as f:
        f.write(out)

if __name__ == '__main__':
    _worker(*sys.argv[1:])