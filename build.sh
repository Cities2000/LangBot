docker stop langbot langbot_plugin_runtime
docker rm langbot langbot_plugin_runtime
docker rmi langbot-custom
docker build -t langbot-custom .
cd docker
docker compose up -d
