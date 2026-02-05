## To build and run locally:

`docker build -t kingish123/sevaro-runner:latest .`

`docker compose up --build sevaro-bot`

Then go to `http://localhost:3267/`

## To build the package and publish to docker hub:

`docker buildx build   --platform linux/amd64,linux/arm64   -t kingish123/sevaro-runner:latest   --push .`

## To pull, build, and run on server:

`sudo docker pull kingish123/sevaro-runner:latest`

`docker compose rm -sf sevaro-bot`

`docker compose up --build -d sevaro-bot`

