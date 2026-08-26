# Vision

Meetup organizers should be able to turn recurring recordings into publishable videos without operating a traditional video editor or micromanaging an agent.

The organizer supplies a folder with camera and/or screen recordings, a digital slide deck when available, and an event-page link when available. The production agent inventories those inputs, separates consecutive talks, asks one consolidated question only for facts it cannot measure, and creates the project manifests. The pipeline reconstructs presentation timing and visuals, keeps the speaker framed with stable camera motion, protects bystanders, makes only high-confidence content edits, and produces long-form video, Shorts, subtitles, FAQ cards, thumbnails, and publishing copy.

Automation must fail safely. A visible bystander is worse than a briefly blurred camera, a wrong content cut is worse than a retained pause, and generated copy must stay grounded in the transcript, slides, and captured event-page context. Every automated decision should remain inspectable through timelines or edit lists, while normal operation should need only answers to genuinely missing intake questions and preview approval.

The project data, EDL, renderer, and detector/OCR command contracts are platform-neutral. Each host smoke-tests its available FFmpeg encoder and records the selected backend in generated metadata; output approval is invalidated when that backend changes. Apple Vision remains the qualified macOS privacy backend. Replacements on Linux or Windows must pass the labeled any-person and overlapping-person recall gates before becoming defaults, and the pipeline fails closed when no qualified detector is available.

Work is parallelized only where artifacts are independent. One run-wide budget limits semantic, GPU, and render workers so nested tools cannot claim the whole machine repeatedly. Distributed queues and GPU-specific filter graphs remain out of scope until measured event throughput requires them.
