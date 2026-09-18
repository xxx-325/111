#!/bin/sh
set -eu

ssh-keygen -A >/dev/null 2>&1
cp /auth/authorized_keys /etc/ssh/authorized_keys
chmod 0644 /etc/ssh/authorized_keys
exec /usr/sbin/sshd -D -e \
  -p 2222 \
  -o ListenAddress=0.0.0.0 \
  -o PasswordAuthentication=no \
  -o UsePAM=no \
  -o KbdInteractiveAuthentication=no \
  -o PermitRootLogin=no \
  -o AllowUsers=sandbox \
  -o DisableForwarding=yes \
  -o GatewayPorts=no \
  -o X11Forwarding=no \
  -o PermitTunnel=no \
  -o AuthorizedKeysFile=/etc/ssh/authorized_keys \
  -o StrictModes=yes \
  -o LogLevel=ERROR
