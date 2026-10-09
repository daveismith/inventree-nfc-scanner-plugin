# The image the server tests run in: an InvenTree release with the test packages added.
#
#   docker build -f tests/Dockerfile --build-arg INVENTREE_VERSION=1.4.3 -t inventree-nfc-test:1.4.3 tests
#
# InvenTree's own image already holds its source and requirements, so the tests import the
# InvenTree of that release. The plugin is not in the image: tests/run.sh mounts the working
# tree and installs it, so a change needs no rebuild. The tests run with no network at all.

ARG INVENTREE_VERSION=1.4.3
FROM ghcr.io/inventree/inventree:${INVENTREE_VERSION}

COPY requirements.txt /tmp/test-requirements.txt
RUN pip install --no-cache-dir -r /tmp/test-requirements.txt

# Settings for a test run: SQLite, no cache server, plugins loaded from their entry points.
ENV INVENTREE_DB_ENGINE=sqlite3 \
    INVENTREE_DB_NAME=/tmp/inventree-test.sqlite3 \
    INVENTREE_DB_HOST= \
    INVENTREE_CACHE_ENABLED=False \
    INVENTREE_CACHE_HOST= \
    INVENTREE_PLUGINS_ENABLED=True \
    INVENTREE_PLUGIN_TESTING=True \
    INVENTREE_PLUGIN_TESTING_SETUP=True \
    INVENTREE_PLUGIN_FILE=/tmp/plugins.txt \
    INVENTREE_AUTO_UPDATE=False \
    INVENTREE_CONSOLE_LOG=False \
    INVENTREE_LOG_LEVEL=WARNING \
    DJANGO_SETTINGS_MODULE=InvenTree.settings \
    PYTHONPATH=/home/inventree/src/backend/InvenTree

WORKDIR /plugin
ENTRYPOINT []
