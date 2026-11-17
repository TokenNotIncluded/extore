# This published base digest is shared by both stages so native libraries match.
ARG ARCH_MIRROR=https://geo.mirror.pkgbuild.com
FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899 AS build

ARG ARCH_MIRROR
RUN printf 'Server = %s/$repo/os/$arch\n' "$ARCH_MIRROR" > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm --needed rust gcc make pkgconf openssl ca-certificates \
    && pacman -Scc --noconfirm
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY docs/schemas/commerce-import.v1.schema.json ./docs/schemas/commerce-import.v1.schema.json
RUN cargo build --release --locked --jobs 4

FROM archlinux:base@sha256:d4594bb2300cba11f41c2f27923eb56f63d9988909c402f9c95517d1e362c899

ARG ARCH_MIRROR
RUN printf 'Server = %s/$repo/os/$arch\n' "$ARCH_MIRROR" > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm --needed python bubblewrap libseccomp openssl gcc-libs ca-certificates shadow \
    && test "$(vercmp "$(pacman -Q bubblewrap | cut -d ' ' -f2)" 0.12)" -ge 0 \
    && pacman -Scc --noconfirm \
    && groupadd --gid 10001 extore \
    && useradd --uid 10001 --gid extore --create-home extore \
    && install -d -o extore -g extore -m 0700 /data

WORKDIR /app
COPY --from=build /build/target/release/extore /usr/local/bin/extore
COPY extore/static ./extore/static
COPY extore/__init__.py extore/variants.py extore/task_flow_definition.py extore/task_flow_schema.py extore/processor_runner.py ./extore/
COPY extore/sdk ./extore/sdk
COPY processors/official/extore_processors ./processors/official/extore_processors

# Only the reviewed standard-library processor SDK is installed, not Python HTTP.
# The Rust worker keeps its existing bubblewrap/seccomp checks and fails closed
# when the container host does not permit its nested unprivileged namespaces.
RUN python3 -I -S -B -c 'import sys; sys.path[:0] = ["/app", "/app/processors/official"]; import extore.sdk, extore_processors' \
    && /usr/local/bin/extore version
ENV EXTORE_DATA=/data \
    EXTORE_STATIC=/app/extore/static \
    EXTORE_LISTEN=0.0.0.0:8000 \
    EXTORE_PROCESSOR_PYTHON=/usr/bin/python3 \
    PYTHONDONTWRITEBYTECODE=1
USER extore
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/extore"]
CMD ["serve"]
