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

# pip install requests

import sys
import json
import time
import requests

# Run the service as
#   docker run --rm -p 5000:5000 cplex/flask_service:12.9
# then the configuration here is correct.
url = 'http://localhost'
port = 5000
model = None
service = None

# Parse command line to overwrite default values and get the model file
# to be solved.
# Optional arguments are
#  -url=<url>
#  -port=<port>
#  -service=<service-url>
# to override the full service URL or overwrite only parts of it.
# Then there is one mandatory argument: the path to the model file
# to be solved.
for arg in sys.argv[1:]:
    if arg.startswith('-url='):
        url = arg[5:]
    elif arg.startswith('-port='):
        port = int(arg[6:])
    elif arg.startswith('-service='):
        service = arg[9:]
    else:
        model = arg

if service is None:
    service = url + ':' + str(port) + '/solve'
print('Service URL: %s' % service)

if model is None:
    raise BaseException('No model')

# Define class that serves as handle to a solve on the remote side.
class Solve(object):
    '''Interface to a solve currently running on the service.

    It encapsulates all the calls to the REST API in plain Python function
    calls.
    '''
    def __init__(self, model_or_id, params = None):
        '''Create a new solve.

        If `model_or_id` is an integer then it is assumed that it refers
        to the ID of a solve that is currently running. In that case
        `params` is just ignored and the new instance is initialized to
        refer to the existing solve.

        In all other cases a new solve is started. The new solve will
        solve the model in the file pointed to by `model_or_id` using
        the parameters in `params` (if any).

        When the newly created instance goes out of scope then it will
        explicitly DELETE the job on the service. To prevent this call
        the `keep()` function before the instance goes out of scope.
        '''
        global service
        self._id = None
        self._keep = False
        test = False
        try:
            self._id = int(model_or_id)
            test = True
        except:
            pass
        if not test:
            files = dict()
            with open(model_or_id, 'rb') as m:
                files['model'] = m
                r = requests.post(service, files = files)
                if r.status_code is not 200:
                    raise BaseException('Failed to create solve: %d (%s)' %
                                        (r.status_code, r.json()))
                self._id = r.json()['id']
        self._url = service + '/' + str(self._id)
        if test:
            self._get(self._url).json()
    def _check(self, r):
        '''Assumes that `r` is a Response object and raises an exception
        if the status code in this response is not 200.
        '''
        if r.status_code is not 200:
            raise BaseException('Invalid request status %d' % r.status_code)
        return r
    def _get(self, *args, **kwargs):
        '''Perform HTTP GET request with given arguments.'''
        return self._check(requests.get(*args, **kwargs))
    def _post(self, *args, **kwargs):
        '''Perform HTTP POST request with given arguments.'''
        return self._check(requests.post(*args, **kwargs))
    def _delete(self, *args, **kwargs):
        '''Perform HTTP DELETE request with given arguments.'''
        return self._check(requests.delete(*args, **kwargs))
    def get_status(self):
        '''Get solve status.

        Returns a JSON object with the status.'''
        return self._get(self._url).json()
    def is_running(self):
        '''Check if the solve is still running.

        Returns a boolean.'''
        return self._get(self._url).json()['status'] == 'RUNNING'
    def interrupt(self):
        '''Interrupt the job and return its current status.

        Note that interrupts may not be immediate. You may have to wait
        a while before the interrupt is acknowledged by the solve. So you
        have to call `get_status()` or `is_running()` until they indicate
        that the solve is complete.'''
        return self._post(self._url + '/interrupt').json()
    def delete(self):
        '''Delete the solve.

        Kills the solve on the remote side and releases all resources the
        remote side allocated for the job.
        '''
        if self._keep: return
        if self._url is None: return
        url = self._url
        self.url = None
        self._delete(url)
    def keep(self, keepjob = True):
        '''Mark the solve to be kept even if this instance goes out of scope.
        '''
        self._keep = keepjob
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        self.delete()

# First get a list of active solves.
# This is not required but illustrates how to get this list.
# The value returned by the service is a list of IDs.
r = requests.get(service)
print('Active solves: '  + str(r.json()))

# Now start the solve, let it run for 10 seconds and then display results.
start = time.time()
with Solve(model) as job:
    print('%-7s %-10s %-10s %s' % ('time', 'dual', 'primal', 'gap'))
    elapsed = time.time() - start
    while job.is_running() and elapsed < 10:
        status = job.get_status()
        elapsed = time.time() - start
        if status['has_incumbent']:
            print('%6.2fs %10f %10f %.2f%% %s' % (elapsed,
                                                  status['dual_bound'],
                                                  status['primal_bound'],
                                                  status['gap'] * 100.,
                                                  status['status']))
        elif 'dual_bound' in status:
            print('%6.2fs %10f %10s %s' % (elapsed, status['dual_bound'],
                                           'unknown',
                                           status['status']))
        else:
            print('%6.2fs %10s %10s %s' % (elapsed, 'unknown', 'unknown',
                                           status['status']))
        if status['status'] == 'COMPLETE':
            break
        time.sleep(1)
    if status['status'] != 'COMPLETE':
        print('Interrupting job ...')
        job.interrupt()
        print('Waiting for job to complete ...')
        while job.is_running():
            time.sleep(1)
    print('Complete:')
    result = job.get_status()
    print('Exit status:  %d' % int(result['exit']))
    print('CPLEX status: %d' % result['statind'])
    print('dual bound:   %f' % result['dual_bound'])
    if result['has_incumbent']:
        print('incumbent:    %f' % result['primal_bound'])
        print('gap:          %.2f%%' % (result['gap'] * 100.))
    else:
        print('infeasible')
    # NOTE: The solution vector is in result['x']
    #       We don't display it here since it may be quite long.
