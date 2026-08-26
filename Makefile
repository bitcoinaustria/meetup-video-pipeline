PYTHON := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
PROJECT ?= video-project.json
RUN := $(PYTHON) scripts/meetup-video.py --project "$(PROJECT)" $(if $(ANALYZER),--analyzer $(ANALYZER)) $(if $(JOBS),--jobs $(JOBS)) $(if $(GPU_JOBS),--gpu-jobs $(GPU_JOBS)) $(if $(RENDER_JOBS),--render-jobs $(RENDER_JOBS))

.PHONY: init capabilities check preview approve copy chapters audio faq shorts validate final release test

init:
	test -n "$(NAME)"
	$(PYTHON) scripts/meetup-video.py --project "projects/$(NAME)/project.json" init --name "$(NAME)" $(if $(EVENT_URL),--event-url "$(EVENT_URL)")

capabilities:
	$(RUN) capabilities

check:
	$(RUN) check

preview:
	$(RUN) preview $(if $(START),--start $(START)) $(if $(DURATION),--duration $(DURATION))

approve:
	$(RUN) approve

copy:
	$(RUN) copy

chapters:
	$(RUN) chapters

audio:
	$(RUN) audio

faq:
	$(RUN) faq

shorts:
	$(RUN) shorts

validate:
	$(RUN) validate

final:
	$(RUN) final

release:
	$(RUN) release

test:
	$(PYTHON) -m py_compile scripts/*.py
	$(PYTHON) scripts/audio-post.py self-test
	$(PYTHON) scripts/build-faq.py --self-test
	$(PYTHON) scripts/build-privacy-review.py --self-test
	$(PYTHON) scripts/build-speaker-track.py self-test
	$(PYTHON) scripts/render-video.py --self-test
	$(PYTHON) scripts/render-shorts.py --self-test
	$(PYTHON) scripts/test-privacy-safety.py
	$(PYTHON) scripts/test-video-common.py
	$(PYTHON) scripts/score-detections.py --self-test
	$(PYTHON) scripts/test-pipeline.py
