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

# tar cvzf - --transform='s,^.*cplex/python/3.6/x86-64_linux/,,' \
#        $COSDIR/cplex/python/3.6/x86-64_linux/cplex \
#        ./service.py \
#        ./Dockerfile \
#        | \
#        docker build \
#        --tag=cplex/flask_service:12.9 \
#        --tag=cplex/flask_service:latest \
#        -f Dockerfile \
#        - || exit 1

FROM python:3.6
MAINTAINER daniel.junglas@de.ibm.com

# Install the CPLEX service binaries.
# This includes the Python program that implements the service and the
# CPLEX Python API for the appropriate Python version.
RUN mkdir -p /opt/CPLEX/cplex
COPY service.py /opt/CPLEX/.
COPY cplex /opt/CPLEX/cplex/.
ENV PYTHONPATH /opt/CPLEX
RUN pip install flask

# Setup users
RUN passwd --delete root && \
	adduser --disabled-password --gecos "" cplex 

# Make the 'cplex' user the owner of everything
RUN chown -R cplex:cplex /home/cplex

USER cplex
WORKDIR /home/cplex


# Default port for flask is 5000
EXPOSE 5000

ENV CPLEX_FLASK_APP yes

CMD python /opt/CPLEX/service.py
