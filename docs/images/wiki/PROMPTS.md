# Wiki banner artwork

The original Wiki banner set includes light and dark artwork based on the approved README Smoked Midnight design. The rounded opaque midnight speaker, cyan/violet lighting and external glass tiles form the shared visual template. The heading, subtitle and external pictograms identify each page.

Generated with the built-in OpenAI image generation tool using the corresponding README theme as an editing reference. Full prompts and reference paths are recorded in [prompts.json](prompts.json); [banners.json](banners.json) records the page catalogue. These are illustrative project artwork, not screenshots, Apple assets or evidence of compatibility or certification.

Generation masters use the template's 2172 × 724 canvas. Publication files are opaque 1800 × 600 JPEGs exported with macOS `sips`, quality 86, preserving the 3:1 composition. This keeps the complete theme set suitable for documentation downloads. Only publication JPEGs are included in manual-install archives.

Wiki pages are maintained directly in the separate GitHub Wiki repository. Copy a page's light/dark pair into its `images/` directory and keep the accessible theme-aware picture header with the page. Add a matching pair and update `scripts/wiki.py` when adding a page. Keep the heading and text available independently of the banner.
