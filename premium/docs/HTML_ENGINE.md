# HTML rendering engines

`PDF_RENDER_MODE=html_template` uses application-owned HTML/CSS and embedded
brand assets. `PDF_HTML_ENGINE` selects `chromium` or `weasyprint` explicitly;
unknown values are rejected and failures never switch engines automatically.
Existing `Settings` default to Chromium. Newly generated shared-service settings
select WeasyPrint 70.0, with Python/native dependencies pinned in the image.

Both engines use the same content, fonts, photographs, cards and 62 mm header.
The diagonal brand plate and icons have explicit SVG geometry/attributes. The
Chromium renderer keeps its browser sandbox, disabled page scripts and blocked
network. The WeasyPrint renderer starts no browser, permits only embedded
`data:` resources and does not fetch local files or external URLs.

The common paginator measures the selected engine's layout and splits overflow
into continuation pages without shrinking the body font or truncating content.
WeasyPrint measurements use public `Page.anchors` targets; its print stylesheet
uses fixed table/block geometry to avoid costly intrinsic grid sizing of long
conditions. Original code pixels are overlaid after composition. Text coverage,
critical values, code decoding and the original-download link are checked before
review. Review remains mandatory.

Artifacts, review checks and `/healthz` identify `html_engine`. Startup validates
the selected renderer without provider calls and logs only safe failure codes.
Changing font libraries or engine versions requires visual and data regression
checks. Hosting walkthroughs are maintained locally outside the repository.

References: [WeasyPrint API](https://doc.courtbouillon.org/weasyprint/v70.0/api_reference.html),
[installation dependencies](https://doc.courtbouillon.org/weasyprint/v70.0/first_steps.html).
