IMAGE := magicboxie-player
MOVIES_DIR := movies
.PHONY: all setup dev build test clean seed-movies pi pi-ssh-deploy pi-ssh-logs pi-ssh-boot-times

all: dev

# --- Docker dev (Mac/other non-Pi machines) -------------------------------
# These targets build/run in Docker for local development off the Pi. For
# the real device, use the `pi-*` targets below instead - the Pi runs the
# daemon natively (systemd + a venv), since Docker buys nothing on a
# single-purpose device and adds overhead the Pi Zero W can't spare.

# Everything here builds/runs in Docker - no local Python/venv needed.
setup:
	@command -v docker >/dev/null || { echo "docker not found - install Docker Desktop / Engine first"; exit 1; }
	docker build --target base -t $(IMAGE):latest .

# Seeds a few sample videos into an empty movies/ dir so there's always
# something to browse in dev - real movie files are large and won't be
# sitting in a fresh checkout. No-ops if movies/ already has content, so
# it's safe to depend on from `dev` every time rather than only once.
# Durations are chosen to land in each of the three MovieCategory buckets.
seed-movies:
	@mkdir -p $(MOVIES_DIR)
	@if [ -z "$$(find $(MOVIES_DIR) -maxdepth 1 -iname '*.mp4' -print -quit)" ]; then \
		echo "No movies found - seeding sample videos into $(MOVIES_DIR)/..."; \
		docker build --target base -t $(IMAGE):latest . >/dev/null; \
		docker run --rm -v "$$(pwd)/$(MOVIES_DIR):/movies" --entrypoint sh $(IMAGE):latest -c '\
			ffmpeg -loglevel error -f lavfi -i "testsrc=duration=6000:size=320x240:rate=1" -y "/movies/Sample Feature Film.mp4" && \
			ffmpeg -loglevel error -f lavfi -i "smptebars=duration=1500:size=320x240:rate=1" -y "/movies/Sample TV Episode.mp4" && \
			ffmpeg -loglevel error -f lavfi -i "testsrc=duration=180:size=320x240:rate=1" -y "/movies/Sample Clip.mp4" \
		'; \
	else \
		echo "Movies already present in $(MOVIES_DIR)/, skipping seed."; \
	fi

# Build for development and run it. `docker compose up --build` recreates the
# container from the freshly built image automatically, so there's nothing to
# manually delete between runs.
dev: seed-movies
	docker compose up --build

# Production image build.
build:
	docker build --target base -t $(IMAGE):latest .

# Runs the test suite inside the same image the app ships in.
test:
	docker build --target test -t $(IMAGE):test .
	docker run --rm $(IMAGE):test

clean:
	docker compose down --rmi local --volumes --remove-orphans 2>/dev/null || true
	docker image rm -f $(IMAGE):latest $(IMAGE):test 2>/dev/null || true

# --- Raspberry Pi -----------------------------------------------------------
# Everything deployed to the device lives in pi/ (see pi/Makefile). These
# forward `make pi`, `make pi-install`, `make pi-logs`, ... from the repo root,
# which is where install.sh and existing checkouts run them.
# `make pi` runs pi/Makefile's `deploy`; `make pi-<target>` runs `<target>`.
pi:
	@$(MAKE) -C pi deploy

pi-%:
	@$(MAKE) -C pi $*

# Deploy from this machine: SSH to the Pi and run `make pi` in its checkout.
# The Pi pulls from origin, so push your commits first - this refuses to run
# if local main has unpushed commits. Override PI_HOST / PI_DIR as needed
# (the checkout keeps its original name, magicboxie-device, on older installs).
# Deploy restarts Wi-Fi, which can drop the SSH session near the end; the
# deploy itself keeps running on the Pi, so check with `make pi-ssh-logs`.
PI_HOST := admin@magicboxie-player.local
PI_DIR := ~/magicboxie-device

pi-ssh-deploy:
	@git fetch -q origin && \
	if [ -n "$$(git log origin/main..HEAD --oneline)" ]; then \
		echo "Unpushed commits - push first, the Pi deploys from origin:"; \
		git log origin/main..HEAD --oneline; exit 1; \
	fi
	ssh -t -o ServerAliveInterval=15 $(PI_HOST) 'cd $(PI_DIR) && make pi'

pi-ssh-logs:
	ssh $(PI_HOST) 'cd $(PI_DIR) && make logs'

pi-ssh-boot-times:
	ssh $(PI_HOST) 'cd $(PI_DIR) && make -C pi boot-times'
