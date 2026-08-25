PYTHON := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
PROJECT ?= video-project.json
RUN := $(PYTHON) scripts/meetup-video.py --project $(PROJECT)

.PHONY: init check preview copy chapters audio faq shorts validate final release

init:
	test -n "$(NAME)"
	$(PYTHON) scripts/meetup-video.py --project projects/$(NAME)/project.json init --name "$(NAME)"

check:
	$(RUN) check

preview:
	$(RUN) preview $(if $(START),--start $(START)) $(if $(DURATION),--duration $(DURATION))

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
