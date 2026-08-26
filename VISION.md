# Vision

Meetup organizers should be able to turn recurring recordings into publishable videos without operating a traditional video editor or micromanaging an agent.

The operator supplies a camera recording, the digital slide deck, and a small project manifest. The pipeline finds the actual presentation start, reconstructs the slide timeline, keeps the speaker framed with stable camera motion, protects bystanders, makes only high-confidence content edits, and produces long-form video, Shorts, subtitles, FAQ cards, thumbnails, and publishing copy.

Automation must fail safely. A visible bystander is worse than a briefly blurred camera, a wrong content cut is worse than a retained pause, and generated copy must stay grounded in the transcript and slides. Every automated decision should remain inspectable through timelines or edit lists, while normal operation should need only a preview approval and one production command.

The current implementation is macOS-first. Detection commands use a platform-neutral TSV contract, and replacements must pass the labeled any-person and overlapping-person recall gates before becoming defaults. The FFmpeg render contract and JSON project data remain platform-neutral in the meantime.
