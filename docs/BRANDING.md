# HomePod Midnight branding

The selected brand for HomePod Sensor Link is the HomePod Midnight Climate Pod: a midnight speaker silhouette, silver outline, white humidity and temperature symbols, and a cyan/violet/pink top. It is original community artwork, not an Apple or Home Assistant logo and does not imply endorsement.

## Packaged assets

Assets live in `custom_components/homepod_sensor_link/brand/` and ship in manual-install archives. Home Assistant 2026.3+ supports this local directory; both supported test versions are newer. No manifest change, CDN upload or upstream submission is required to include these local assets. See the [Home Assistant announcement](https://contributors.home-assistant.io/blog/2026/02/24/brands-proxy-api/) and [image specifications](https://github.com/home-assistant/brands#image-specification).

| Use | Standard transparent PNG | High-resolution transparent PNG |
| --- | --- | --- |
| Light icon | [icon.png](../custom_components/homepod_sensor_link/brand/icon.png), 256 × 256 | [icon@2x.png](../custom_components/homepod_sensor_link/brand/icon@2x.png), 512 × 512 |
| Dark icon | [dark_icon.png](../custom_components/homepod_sensor_link/brand/dark_icon.png), 256 × 256 | [dark_icon@2x.png](../custom_components/homepod_sensor_link/brand/dark_icon@2x.png), 512 × 512 |
| Light logo | [logo.png](../custom_components/homepod_sensor_link/brand/logo.png), 256 × 256 | [logo@2x.png](../custom_components/homepod_sensor_link/brand/logo@2x.png), 512 × 512 |
| Dark logo | [dark_logo.png](../custom_components/homepod_sensor_link/brand/dark_logo.png), 256 × 256 | [dark_logo@2x.png](../custom_components/homepod_sensor_link/brand/dark_logo@2x.png), 512 × 512 |

The brand uses the symbol itself as its square logo. The same midnight artwork intentionally serves both themes: the silver outline separates the body from charcoal, while white glyphs remain visible inside the dark body. Explicit logo and dark filenames are provided as equivalent copies at each resolution. There is no generated wordmark in the shipped set. Home Assistant also supports falling back to icons when separate logo files are omitted.

## README banners

The README uses a theme-aware `picture` element with the selected [dark banner](images/readme-banner-dark.png), a matching [light banner](images/readme-banner-light.png), and the light image as its fallback. Both are 2172 × 724 opaque PNGs displayed at up to 1200 pixels wide. The selected border-free midnight speaker remains dark in both versions; the background, type and external sensor tiles adapt to the theme. The README PNGs ship with the manual-install archive.

The light version was generated from the selected dark banner using the built-in image-generation tool. Its prompt is retained in `docs/images/BANNER_PROMPTS.md`. Earlier concepts and the original SVG are preserved as historical artwork; the SVG is no longer referenced by the README. Integration icons remain separate and unchanged.

## Source and exports

The built-in image-generation tool refined the selected concept. The unmodified source master and generation prompts are retained under `docs/images/brand-source/`; earlier concepts remain under `docs/images/icon-concepts/`. Distribution PNGs were resized with macOS `sips`, preserving alpha. The source and preview files are intentionally excluded from the install archive.

To reproduce the two sizes on macOS:

```sh
sips --resampleHeightWidth 256 256 docs/images/brand-source/midnight-icon.png --out custom_components/homepod_sensor_link/brand/icon.png
sips --resampleHeightWidth 512 512 docs/images/brand-source/midnight-icon.png --out custom_components/homepod_sensor_link/brand/icon@2x.png
```

Copy each exported icon to its matching dark-icon, logo and dark-logo filename. Keep every copy byte-identical at the same resolution. Review the actual exports on white and charcoal at 32, 48, 128 and 256 CSS pixels. The source is generated raster artwork; it is not an editable vector master.

Local assets and package checks are separate from hosted CI, upstream brand review, marketplace availability and installed-Home-Assistant verification. None of those external outcomes is claimed by adding these files, and the existing release-evidence gates remain unchanged.
