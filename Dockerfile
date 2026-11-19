FROM docker.io/library/python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 AS seekgnomes-control
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    HOME=/tmp/seekgnomes-home LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends python3-minimal \
    bubblewrap libseccomp2 util-linux git poppler-utils ca-certificates libgomp1 libstdc++6 xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/user /home/ubuntu/Mooke/Eureka/Environment/Python \
    && python -m venv --copies /home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3
COPY science-wheels /tmp/seekgnomes-science-wheels
COPY requirements-science.lock /tmp/seekgnomes-science.lock
RUN /home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3/bin/python -m pip install \
    --no-index --require-hashes --find-links=/tmp/seekgnomes-science-wheels -r /tmp/seekgnomes-science.lock \
    && rm -rf /tmp/seekgnomes-science-wheels /root/.cache
# Harbor dependencies are separate from the frozen scientific interpreter's
# site-packages. Strict Python/Lean child launchers use -I and do not inherit it.
COPY harbor-wheels /tmp/seekgnomes-harbor-wheels
COPY requirements-harbor.lock /tmp/seekgnomes-harbor.lock
RUN python -m pip install --no-index --require-hashes \
    --find-links=/tmp/seekgnomes-harbor-wheels --target=/opt/seekgnomes/harbor-deps \
    -r /tmp/seekgnomes-harbor.lock \
    && rm -rf /tmp/seekgnomes-harbor-wheels /root/.cache
FROM seekgnomes-control AS seekgnomes-native
COPY harness-assets /tmp/seekgnomes-harness-assets
COPY verify-harness-assets.py /tmp/verify-harness-assets.py
RUN python /tmp/verify-harness-assets.py /tmp/seekgnomes-harness-assets \
    && bash /tmp/seekgnomes-harness-assets/install.sh \
    && mkdir -p /opt/seekgnomes \
    && cp /tmp/seekgnomes-harness-assets/manifest.json /opt/seekgnomes/harness-assets.json \
    && rm -rf /tmp/seekgnomes-harness-assets /tmp/verify-harness-assets.py /root/.cache
FROM seekgnomes-control AS seekgnomes-runtime
COPY --from=seekgnomes-native /opt/seekgnomes/harness /opt/seekgnomes/harness
COPY --from=seekgnomes-native /opt/seekgnomes/harness-assets.json /opt/seekgnomes/harness-assets.json
COPY runtime /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/tmp/lean-runtime
COPY mathlib-packages /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/lean/.lake/packages
COPY payload /home/ubuntu/Mooke/Eureka
ENV PYTHONPATH=/opt/seekgnomes/harbor-deps \
    PATH=/opt/seekgnomes/harness/bin:/home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3/bin:/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/tmp/lean-runtime/lean-4.35.0-linux/bin:/usr/local/bin:/usr/bin:/bin
WORKDIR /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/bin/python3 -I -c 'import argparse,ctypes,http.server,json,pathlib,resource,select,socket,struct,subprocess,threading,time,urllib.request; ctypes.CDLL("libseccomp.so.2"); print("role-bridge stdlib and libseccomp: ok")' \
    && git config --system --add safe.directory /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/lean/.lake/packages/mathlib \
    && mkdir -p runs \
    && chmod 1777 runs tmp \
    && chmod -R a+rX /home/ubuntu/Mooke/Eureka
USER 1000:1000
ENTRYPOINT ["/home/ubuntu/Mooke/Eureka/Environment/Python/miniconda3/bin/python", "-B", "-m", "revisions.seekgnomes_v1_2.cli"]
CMD []
