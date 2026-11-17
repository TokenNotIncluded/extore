# Build on a roomy runner; production never needs to compile the Rust graph.
FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899

ARG ARCH_MIRROR=https://geo.mirror.pkgbuild.com
RUN printf 'Server = %s/$repo/os/$arch\n' "$ARCH_MIRROR" > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm --needed base-devel rust python bubblewrap libseccomp openssl ca-certificates curl \
    && useradd --uid 10001 --create-home builder

# Production uses OpenSSL 3.6.5. Keep its signed headers/libraries in a private
# prefix; do not downgrade the builder's own OpenSSL or other system packages.
RUN curl --fail --location --retry 5 --retry-all-errors --continue-at - --connect-timeout 20 --max-time 240 \
      https://archive.archlinux.org/packages/o/openssl/openssl-3.6.5-1-x86_64.pkg.tar.zst \
      --output /tmp/extore-openssl.pkg.tar.zst \
    && curl --fail --location --retry 5 --retry-all-errors --continue-at - --connect-timeout 20 --max-time 120 \
      https://archive.archlinux.org/packages/o/openssl/openssl-3.6.5-1-x86_64.pkg.tar.zst.sig \
      --output /tmp/extore-openssl.pkg.tar.zst.sig \
    && printf '%s  %s\n' be0a07ef3d8ffa878732b138e98dee26559527212cb941fc5f6c282545c787cc /tmp/extore-openssl.pkg.tar.zst | sha256sum --check --strict \
    && pacman-key --verify /tmp/extore-openssl.pkg.tar.zst.sig /tmp/extore-openssl.pkg.tar.zst \
    && bsdtar -xOf /tmp/extore-openssl.pkg.tar.zst .PKGINFO > /tmp/extore-openssl.PKGINFO \
    && grep -Fx 'pkgname = openssl' /tmp/extore-openssl.PKGINFO \
    && grep -Fx 'pkgver = 3.6.5-1' /tmp/extore-openssl.PKGINFO \
    && install -d /opt/extore-openssl3 \
    && bsdtar -xf /tmp/extore-openssl.pkg.tar.zst -C /opt/extore-openssl3 \
    && rm /tmp/extore-openssl.pkg.tar.zst /tmp/extore-openssl.pkg.tar.zst.sig /tmp/extore-openssl.PKGINFO

ENV OPENSSL_DIR=/opt/extore-openssl3/usr \
    LD_LIBRARY_PATH=/opt/extore-openssl3/usr/lib
WORKDIR /work
