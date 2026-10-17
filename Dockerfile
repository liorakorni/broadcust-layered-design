# x86_64 deliberately, not the arm64 used by the sibling repos: Playwright's
# Chromium builds are far better supported on x86_64 (SPEC section 3).
FROM public.ecr.aws/lambda/python:3.12

# Corporate TLS-intercepting proxies (Cato, Zscaler and the like) re-sign every
# certificate, so dnf, pip and the Playwright downloader all fail to verify the
# upstream CDNs from inside the build. Drop any such root CA into
# assets/ca-certificates/*.crt and it is trusted here. The directory is kept in
# git but the certificates are not: they are machine-specific. With no .crt
# present this whole step is a no-op.
COPY assets/ca-certificates/ /etc/pki/ca-trust/source/anchors/
RUN update-ca-trust extract \
    && printf 'sslverify=1\n' >> /etc/dnf/dnf.conf
ENV SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    NODE_EXTRA_CA_CERTS=/etc/pki/tls/certs/ca-bundle.crt \
    PIP_CERT=/etc/pki/tls/certs/ca-bundle.crt

# Chromium is baked in at build time and must be readable by the Lambda runtime
# user. The Playwright default (~/.cache/ms-playwright) is not writable in Lambda,
# so pin an explicit location used at both build and run time.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    FONTCONFIG_PATH=/etc/fonts \
    XDG_CACHE_HOME=/tmp/.cache \
    HOME=/tmp

# Chromium's shared-library dependencies. `playwright install --with-deps` only
# knows apt, and this base image is Amazon Linux 2023, so install them explicitly.
RUN dnf install -y \
      alsa-lib \
      at-spi2-atk \
      at-spi2-core \
      atk \
      cairo \
      cups-libs \
      dbus-libs \
      expat \
      fontconfig \
      freetype \
      gtk3 \
      libX11 \
      libXcomposite \
      libXdamage \
      libXext \
      libXfixes \
      libXrandr \
      libxcb \
      libxkbcommon \
      mesa-libgbm \
      nspr \
      nss \
      pango \
    && dnf clean all \
    && rm -rf /var/cache/dnf

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

# Install the browser binary itself. The Playwright python package version in
# requirements.txt determines which Chromium build this pulls, so the two must be
# bumped together.
RUN PYTHONPATH="${LAMBDA_TASK_ROOT}" python -m playwright install chromium \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# Fonts. A font-family name in CSS does not mean the font is installed; Heebo is
# vendored in the repo and registered with fontconfig here. A missing font is a
# blocking render error, never a warning (SPEC section 5).
COPY assets/fonts/ /usr/share/fonts/layered-design/
RUN fc-cache -f \
    && fc-list | grep -qi heebo || (echo "Heebo not registered with fontconfig" && exit 1)

COPY conf.py handler_api.py handler_stage.py ${LAMBDA_TASK_ROOT}/
COPY lib/ ${LAMBDA_TASK_ROOT}/lib/
COPY prompts/ ${LAMBDA_TASK_ROOT}/prompts/

CMD ["handler_api.handler"]
