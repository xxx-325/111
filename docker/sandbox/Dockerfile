FROM python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git=1:2.39.5-0+deb12u3 \
       openssh-server=1:9.2p1-2+deb12u10 \
       tmux=3.3a-3 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir \
       myst-parser==4.0.1 \
       pallets-sphinx-themes==2.3.0 \
       pytest==8.4.2 \
       sphinx==8.2.3 \
       sphinx-tabs==3.4.7 \
       sphinxcontrib-log-cabinet==1.0.1 \
    && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
    && usermod --password '*' sandbox \
    && mkdir -p /run/sshd /home/sandbox/.ssh \
    && chown -R sandbox:sandbox /home/sandbox/.ssh \
    && chmod 0700 /home/sandbox/.ssh

COPY entrypoint.sh /usr/local/bin/sandbox-entrypoint
RUN chmod 0755 /usr/local/bin/sandbox-entrypoint
RUN rm -f /etc/ssh/ssh_host_rsa_key /etc/ssh/ssh_host_rsa_key.pub \
    /etc/ssh/ssh_host_ecdsa_key /etc/ssh/ssh_host_ecdsa_key.pub \
    /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub

EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint"]
