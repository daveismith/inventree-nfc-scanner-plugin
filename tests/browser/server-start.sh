#!/bin/bash
# The InvenTree server of the browser stack: the plugin installed from the mounted working
# tree, the database migrated, static files collected (InvenTree's and the plugin's, served
# by the server itself through whitenoise), then gunicorn.
#
# On an empty database plugins load only once InvenTree's own tables exist, so the plugin's
# tables come in a second step: any command after the first migrate starts with the plugin
# loaded, and InvenTree's start-up check (INVENTREE_AUTO_UPDATE, as in its own Docker setup)
# migrates its app. Static files are collected after that, when the plugin's are found too.
# Both happen before gunicorn starts, so its workers find nothing to migrate.
# Not `invoke migrate`: it runs makemigrations, which could write into the mounted tree.
set -e
cd /home/inventree
pip install --quiet --no-deps --no-build-isolation --no-index -e /plugin 2>&1 | grep -v "as the 'root' user" || true
manage() { (cd src/backend/InvenTree && python3 manage.py "$@"); }
manage migrate --no-input -v 0
manage check_migrations
invoke static
exec gunicorn -c ./gunicorn.conf.py InvenTree.wsgi -b 0.0.0.0:8000 --chdir src/backend/InvenTree
