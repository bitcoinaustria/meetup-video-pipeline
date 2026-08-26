# Vision

Meetup organizers should be able to turn recurring recordings into publishable videos without operating a traditional video editor or micromanaging an agent.

The organizer supplies a folder with the camera recording and digital slide deck, plus an event-page link when available. The production agent inventories those inputs, asks only for missing context, and creates the project manifest. The pipeline finds the actual presentation start, reconstructs the slide timeline, keeps the speaker framed with stable camera motion, protects bystanders, makes only high-confidence content edits, and produces long-form video, Shorts, subtitles, FAQ cards, thumbnails, and publishing copy.

Automation must fail safely. A visible bystander is worse than a briefly blurred camera, a wrong content cut is worse than a retained pause, and generated copy must stay grounded in the transcript, slides, and captured event-page context. Every automated decision should remain inspectable through timelines or edit lists, while normal operation should need only answers to genuinely missing intake questions and preview approval.

The current implementation is macOS-first. Detection commands use a platform-neutral TSV contract, and replacements must pass the labeled any-person and overlapping-person recall gates before becoming defaults. The FFmpeg render contract and JSON project data remain platform-neutral in the meantime.
