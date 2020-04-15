#   Copyright 2020 IBM Corporation
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# A very small and tiny service for CPLEX.
# The service consumes a model and solves it.
# The design is super simple:
# - We have a main process that runs flask.
# - Posting to /solve will create a new solve.
#   For each solve we create a new sub-process, so that the main process
#   can return right after starting the solve. For process creation we
#   directly interface with the os package. That is not exactly portable,
#   but we know we are on Linux so we can rely on some low-level stuff.
#   Each process gets assigned a temporary directory into which it writes
#   progress information.
#   Each solve is identifed py the process id (pid) of the newly created
#   process.
# - Any further interaction with the solve happens through the master
#   and through URL solve/PID. The master process will either interact
#   with the process object or will pull information from the temporary
#   directory.

# pip install flask
import os
import sys
import uuid
import fcntl
import cplex
import signal
import tempfile
from flask import Flask, request, jsonify, abort

app = Flask('CPLEX service')
app.config['DEBUG'] = False
app.cpxprocs = dict()


# ###################################################################### #
#                                                                        #
#    Utilities                                                           #
#                                                                        #
# ###################################################################### #

class LockedDirectory(object):
    '''Wrapper for locked directories.

    A locked diretory is represented by the path to this directory and
    an open file descriptor that implements the actual lock.
    the `open` function of this class returns an open file descriptor
    for a new file in this directory that will be automatically closed.

    Instances of this class are used as context managers. Once the context
    manager exits, all file descriptors returned by `open` are automatically
    closed. Also the file descriptor passed to the constructor is closed,
    thus releasing the lock that was held on that descriptor.
    '''
    def __init__(self, path, lockfd):
        self._path = path
        self._lockfd = lockfd
        self._handles = list()
    def open(self, name, mode):
        fd = open(os.path.join(self._path, name), mode)
        self._handles.append(fd)
        return fd
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        handles = self._handles
        self._handles = list()
        for fd in handles:
            fd.close()
        self._lockfd.close()


class ProcessDirectory(object):
    '''Instances of this class represent the temporary directory that we
    create for each process that we spawn.
    '''
    def __init__(self, path = None, suffix = None, prefix = None, dir = None):
        self._obj = None
        if path is None:
            # NOTE: Here we store everything in a random temporary directory.
            #       If we wanted to implement failover, we could create the
            #       temp directory on a persistent storage (volume). And could
            #       implement
            #       a failover strategy like this
            #       - when a model is succesfully solved, then the model file
            #         is removed from the directory (or the whole directory is
            #         removed).
            #       - when the services starts and there are still model files
            #         in directories then this means the previous service
            #         crashed. In that case the service restarts a new process
            #         with that model.
            #         The 'pid' file in the temporary directory can be used to
            #         find back the pid that was communicated to the client.
            self._obj = tempfile.TemporaryDirectory(suffix, prefix, dir)
            self._path = os.path.abspath(self._obj.name)
            self._lockfile = os.path.join(self._path, '__lock__')
            try:
                # Create the lockfile
                with open(self._lockfile, 'w'): pass
            except:
                self._obj.cleanup()
                raise

        else:
            self._path = os.path.abspath(path)
            self._lockfile = os.path.join(self._path, '__lock__')
            # Make sure the lockfile exists
            with open(self._lockfile, 'r'): pass
    def disable_cleanup(self):
        if self._obj is not None:
            # HACK: To disable the auto-cleanup of TemporaryDirectory, we
            #       just replace the cleanup function by a noop.
            self._obj.cleanup = lambda : None
    def cleanup(self):
        if self._obj is not None:
            self._obj.cleanup()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()
    def _lock(self, fmode, lmode):
        fd = open(self._lockfile, fmode)
        try:
            fcntl.lockf(fd, lmode)
            return LockedDirectory(self._path, fd)
        except:
            fd.close()
            raise
    def lock_write(self):
        return self._lock('w', fcntl.LOCK_EX)
    def lock_read(self):
        return self._lock('r', fcntl.LOCK_SH)
    def make_name(self, name):
        return os.path.join(self._path, name)

# ###################################################################### #
#                                                                        #
#    Child process - solve a model                                       #
#                                                                        #
# ###################################################################### #

class InfoCallback(cplex.callbacks.MIPInfoCallback):
    '''Info callback.

    This is invoked periodically by CPLEX and stores the current state of
    the solve on disk.
    '''
    def __call__(self):
        with self.procdir.lock_write() as d:
            gap = float('inf')
            if (self.has_incumbent()):
                with d.open('primal', 'w') as f:
                    f.write('%f\n' % self.get_incumbent_objective_value())
                with d.open('x', 'w') as f:
                    for v in self.get_incumbent_values():
                        f.write('%f\n' % v)
                gap = self.get_MIP_relative_gap()
            with d.open('dual', 'w') as f:
                f.write('%f\n' % self.get_best_objective_value())
            with d.open('progress', 'w') as f:
                f.write('%f\n' % gap)
                f.write('%d\n' % self.get_num_iterations())
                f.write('%d\n' % self.get_num_nodes())
                f.write('%d\n' % self.get_num_remaining_nodes())

def solve_model(procdir, model, argfiles):
    '''Solve a model.

    This is the function that is called by the child process and actually
    solves a model.
    `procdir` is the process's temporary directory.
    `files` is the files object from the request that contains all the files
    that were uploaded with the request.
    '''
    try:
        with cplex.Cplex() as cpx:
            with open(procdir.make_name('log'), 'w') as logfile:
                # Redirect log to file. Redirect to None to disable log
                cpx.set_results_stream(logfile)
                cpx.set_warning_stream(logfile)
                cpx.set_error_stream(logfile)
                cpx.set_log_stream(logfile)

                # Read model file
                cpx.read(model)

                # Read parameters (if present)
                if 'params' in argfiles:
                    cpx.parameters.read_file(argfiles['params'])

                # This can be used to have CPLEX track incumbent solutions
                # on disk.
                # cpx.parameters.output.intsolfileprefix.set(procdir.make_name('incumbent'))

                # Register an info callback that periodically captures the
                # current state of the solve on disk.
                cb = cpx.register_callback(InfoCallback)
                cb.procdir = procdir

                # Install an aborter (hooked to SIGUSR1).
                # Aborting with an aborter is sometimes faster than with Ctrl-C
                # and it also does not result in 'KeyboardInterrupt' being
                # printed on the console.
                with cplex.Aborter() as abrt:
                    signal.signal(signal.SIGUSR1, lambda a,b: abrt.abort())
                    # Trigger the solve.
                    cpx.solve()

                    # Capture the final results of the solve.
                    # We are lazy here: Instead of testing whether certain
                    # information is available, we just attempt to query the
                    # information and silently swallow exceptions.
                    sol = cpx.solution
                    with procdir.lock_write() as d:
                        try:
                            with d.open('statind', 'w') as f:
                                f.write('%d\n' % sol.get_status())
                        except: pass

                        try:
                            with d.open('primal', 'w') as f:
                                f.write('%f\n' % sol.get_objective_value())
                        except: pass
                        try:
                            with d.open('x', 'w') as f:
                                for v in sol.get_values():
                                    f.write('%f\n' % v)
                        except: pass

                        try:
                            with d.open('dual', 'w') as f:
                                f.write('%f\n' % sol.MIP.get_best_objective())
                        except: pass

                        progress = sol.progress
                        try:
                            with d.open('progress', 'w') as f:
                                f.write('%f\n' % sol.MIP.get_mip_relative_gap())
                                f.write('%d\n' % progress.get_num_iterations())
                                f.write('%d\n' % progress.get_num_nodes_processed())
                                f.write('%d\n' % progress.get_num_nodes_remaining())
                        except: pass
    except:
        # Any unexpected exception in the child gets here.
        # We save the exception message and then re-raise the exception.
        try:
            with procdir.lock_write() as d:
                with d.open('exception', 'w') as f:
                    f.write(str(sys.exc_info()[0]))
        except:
            # Ignore any exception during exception handling
            pass
        raise

# ###################################################################### #
#                                                                        #
#    Master process, the service REST interface implementation           #
#                                                                        #
# ###################################################################### #

class ServiceError(ValueError):
    def __init__(self, status, message):
        self.status = status
        self.message = message

def service_error(status, message):
    print('SERVICE ERROR %s' % str(message))
    response = jsonify({'message': str(message) })
    response.status_code = status
    return response

@app.errorhandler(ServiceError)
def service_error_handler(error):
    return service_error(error.status, error.message)

class CplexHandle(object):
    '''Class to store information about a solve in the master process.

    Instances of this class serve as handle to the solve that runs in a
    different process.
    '''
    def __init__(self, proc_dir, pid, proc_id):
        self._pid = pid
        self._proc_id = proc_id
        self._proc_dir = proc_dir
        self._status = { 'proc_dir': self._proc_dir.make_name(''), # FIXME: This is only for debugging
                         'pid': self._pid, # FIXME: This is only for debugging
                         'id' : self._proc_id
                         }
        self._waited = False

    def _wait(self, block = True):
        '''Return True if child completed.'''
        if hasattr(self, '_exit_status'):
            return True
        if self._waited:
            return
        if block:
            (pid, status) = os.waitpid(self._pid, 0)
            self._waited = True
        else:
            (pid, status) = os.waitpid(self._pid, os.WNOHANG)
            if pid == 0:
                return False
            self._waited = True
        if os.WIFSIGNALED(status):
            self._termsig = os.WTERMSIG(status)
        else:
            self._termsig = 0
        if os.WIFEXITED(status):
            self._exit_status = os.WEXITSTATUS(status)
        else:
            self._exit_status = status
    def interrupt(self):
        '''Interrupt the solve.
        Note that it may take a moment before the child process acknowledges
        the interrupt and the solve actually stops.
        '''
        try:
            # Interrupt: SIGUSR1 to trigger the aborter,
            # SIGINT to mimick Ctrl-C
            os.kill(self._pid, signal.SIGUSR1)
            os.kill(self._pid, signal.SIGINT)
        except:
            # Ignore exceptions. We can send multiple interrupts
            pass
    def kill(self, signo = signal.SIGTERM):
        '''Forcibly kill the child process.'''
        try:
            os.kill(self._pid, signo)
        except:
            # Ignore exceptions. We can kill multitple times
            pass
    def alive(self):
        '''Test whether the corresponding child process is still alive.'''
        # On Linux we can test whether a process is alive by sending it
        # the signal 0. This will fail if the process does not exist
        #try:
        #    os.kill(self._pid, 0)
        #    return True
        #except:
        #    return False
        return not self._wait(False)

    def _get_info(self, d, key, fields):
        if self._proc_dir is not None:
            status = self._status
            try:
                with d.open(key, 'r') as f:
                    for field in fields:
                        status[field[1]] = field[0](f.readline())
                return True
            except:
                # Ok, file may not exist
                pass
        return False

    def get_status(self):
        '''Get the status of the corresponding solve.

        Returns a dictionary with the current state of the solve.
        '''
        if self._proc_dir is None:
            return self._status
        status = self._status
        with self._proc_dir.lock_read() as d:
            # Get information about primal bound (if present)
            status['has_incumbent'] = self._get_info(d, 'primal', \
                                                     [(float, \
                                                       'primal_bound')])
            # Get best x vector (if present)
            try:
                x = []
                with d.open('x', 'r') as f:
                    for line in f:
                        x.append(float(line))
                        status['x'] = x
            except: pass

            # Read dual bound (if present)
            self._get_info(d, 'dual', [(float, 'dual_bound')])

            # Read progress information (if present)
            self._get_info(d, 'progress', [(float, 'gap'),
                                           (int, 'iterations'),
                                           (int, 'nodes'),
                                           (int, 'open_nodes')])
            # If the solve completed, then there is a statind file
            # with the CPLEX exit status
            self._get_info(d, 'statind', [(int, 'statind')])

            # Capture exception if there was one.
            try:
                with d.open('exception', 'r') as f:
                    status['exception'] = '\n'.join(f.readlines())
            except: pass
        if self.alive():
            status['status'] = 'RUNNING'                     
        else:
            self._wait()
            status['status'] = 'COMPLETE'
            status['exit'] = self._exit_status
        return status

    def cleanup(self):
        if self._proc_dir is not None:
            proc_dir = self._proc_dir
            self._proc_dir = None
            proc_dir.cleanup()

def get_handle(proc_id):
    '''Get the handle for a particular pid.

    If `proc_id` does not refer to an existing solve then the current request
    is aborted.
    '''
    handle = app.cpxprocs.get(proc_id, None)
    if handle is None:
        raise ServiceError(404,  # not found
                           'No solve with id %s' % proc_id)
    return handle

def save_file(files, name, proc_dir, exists = False):
    '''Save an uploaded file on disk.

    Reads the file `name` from `files` and stores it in directory `abstmp`.
    If `exists` is True then it is assumed that `name` exists in `files`.

    Returns the absolute path of the local file or None if the file did
    not exist in `files`.'''
    if not exists:
        if not name in files:
            return None
    f = files[name]
    dirname = proc_dir.make_name(name)
    os.mkdir(dirname, 0o700)
    filename = os.path.join(dirname, f.filename)
    f.save(filename)
    return filename

@app.route('/solve', methods = ['GET', 'POST', 'DELETE'])
def solve_main():
    '''Start a new solve or get list of active solves.

    GET: Gets a list of all currently active solves.
    POST: Starts a new solve and returns the pid for that new solve.
          The POST data must contain at least one argument: 'model'.
          This must contain the model file to solve.
          Other optional files that can be uploaded are
          'params': A CPLEX parameter file
    DELETE: Kill and delete all running solves
    '''

    if request.method == 'GET':
        return jsonify(sorted(app.cpxprocs))

    if request.method == 'DELETE':
        handles = app.cpxprocs
        app.cpxprocs = dict()
        solves = list()
        for h in handles:
            try:
                h.kill(signal.SIGKILL)
                h._wait()
                solves.append(h.get_status())
                h.cleanup()
            except:
                pass
        return jsonify(solves)

    if not 'model' in request.files:
              raise ServiceError(400,  # Bad request
                                 'No model')

    if len(app.cpxprocs) > 0:
        # At the moment we allow only one active solve. Just to keep
        # things under control.
              raise ServiceError(403,  # Forbidden
                                 'Too many solves')

    proc_id = str(uuid.uuid4())

    # Create temporary directory for sub process to be created.
    # NOTE: We do NOT use the context manager semantics here as the end
    #       of the context manager will remove the directory!
    #       Instead we explicitly call cleanup() in CplexHandle.__del__()
    proc_dir = ProcessDirectory(suffix = '', prefix = 'cpx')

    try:
        # Save all files that were uploaded with the request
        model = save_file(request.files, 'model', proc_dir, True)
        argfiles = dict()
        for name in ['params']:
            filename = save_file(request.files, name, proc_dir)
            if filename is not None:
                argfiles[name] = filename

        # Fork the new process
        newpid = os.fork()
        if newpid == 0:
            # Child process
            # ATTENTION: Since we forked the process, we have two instances
            #            of ProcessDirectory now! If any of the
            #            two goes out of scope, then the directory will be
            #            removed. So in the child we must disable the auto
            #            cleanup.
            proc_dir.disable_cleanup()
            solve_model(proc_dir, model, argfiles)
            sys.exit(0)
        else:
            # Parent
            try:
                app.cpxprocs[proc_id] = CplexHandle(proc_dir, newpid, proc_id)
                with proc_dir.lock_write() as d:
                    with d.open('pid', 'w') as f:
                        f.write('%d' % newpid)
                    with d.open('id', 'w') as f:
                        f.write('%s' % proc_id)
                return jsonify({'pid': newpid, # FIXME: This is only for debug
                                'id' : proc_id})
            except:
                # Something went wrong when returning results to the client.
                # Kill the child.
                os.kill(newpid, signal.SIGKILL)
                os.waitpid(newpid, 0)
                raise
    except SystemExit:
        raise
    except ServiceError:
        raise
    #except:
    #    proc_dir.cleanup()
    #    return service_error(500, sys.exc_info()[0])

@app.route('/solve/<string:solve>', methods = ['GET', 'DELETE'])
def solve_status(solve):
    '''Get current status of a solve or delete it.
    GET returns a JSON dictionary with the status of the solve.
    DELETE deletes the solve and all resources allocated to it.
    '''
    try:
        if request.method == 'GET':
            handle = get_handle(solve)
            return jsonify(handle.get_status())
        else:
            handle = get_handle(solve)
            handle.kill(signal.SIGKILL)
            handle._wait()
            handle.cleanup()
            del app.cpxprocs[solve]
            return jsonify({'solve' : solve})
    except ServiceError:
        raise
    except:
        return service_error(500, sys.exc_info()[0])

@app.route('/solve/<string:solve>/interrupt', methods = ['POST'])
def solve_interrupt(solve):
    '''Interrupt the current solve (similar to Ctrl-C).

    Returns a JSON object with the current status of the solve.
    The function does NOT wait until the process acknowledges the interrupt!
    It returns immediately. You will have to poll the process's status until
    it reports the process complete.
    '''
    try:
        handle = get_handle(solve)
        handle.interrupt()
        return jsonify(handle.get_status())
    except ServiceError:
        raise
    except:
        return service_error(500, sys.exc_info()[0])

@app.route('/solve/<string:solve>/terminate', methods = ['POST'])
def solve_terminate(solve):
    '''Forcibly kill the solve.

    This is a bit more graceful than calling kill.
    Returns a JSON dictionary with the current status of the solve.
    The function does NOT wait until the process acknowledges the signal!
    It returns immediately. You will have to poll the process's status until
    it reports the process complete.
    '''
    try:
        handle = get_handle(solve)
        handle.kill(15)
        handle._wait()
        return jsonify(handle.get_status())
    except ServiceError:
        raise
    except:
        return service_error(500, sys.exc_info()[0])

@app.route('/solve/<string:solve>/terminate', methods = ['POST'])
def solve_kill(solve):
    '''Forcibly kill the solve.

    Returns a JSON dictionary with the current status of the solve.
    The function does NOT wait until the process acknowledges the signal!
    It returns immediately. You will have to poll the process's status until
    it reports the process complete.
    '''
    try:
        handle = get_handle(solve)
        handle.kill(9)
        handle._wait()
        return jsonify(handle.get_status())
    except ServiceError:
        raise
    except:
        return service_error(500, sys.exc_info()[0])

if __name__ == '__main__':
    if 'CPLEX_FLASK_APP' in os.environ:
        app.run(debug = False, host = '0.0.0.0') # for running under docker
    else:
        app.run()

