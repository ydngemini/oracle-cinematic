# The perf image = the local backend image + the CURRENT backend/requirements.txt.
# The local oracle-backend:latest can lag requirements (Mission 8 found plivo,
# telnyx and pynacl missing — the webhook verifiers' own dependencies), and a
# load test must run what production builds. pip only adds what is missing.
FROM oracle-backend:latest
USER root
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Back to the base image's unprivileged user.
USER oracle
