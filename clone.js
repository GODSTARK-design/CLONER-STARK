// api/clone.js
const axios = require("axios");
const cheerio = require("cheerio");
const JSZip = require("jszip");
const path = require("path");
const mime = require("mime-types");
const pLimit = require("p-limit");

const USER_AGENT = "WebClonerAPI/1.0 (+https://yourdomain.example)";

function absoluteUrl(base, relative) {
  try {
    return new URL(relative, base).href;
  } catch (e) {
    return null;
  }
}

function sanitizeFilename(urlStr, fallbackIndex = 0) {
  try {
    const u = new URL(urlStr);
    let name = path.basename(u.pathname) || `asset-${fallbackIndex}`;
    name = name.split("?")[0].split("#")[0];
    const ext = path.extname(name);
    if (!ext) {
      const mimeType = mime.lookup(u.pathname) || "";
      const guessedExt = mime.extension(mimeType) ? `.${mime.extension(mimeType)}` : "";
      name += guessedExt;
    }
    return name.replace(/[^a-zA-Z0-9.\-_]/g, "_");
  } catch (e) {
    return `asset_${fallbackIndex}`;
  }
}

async function fetchBinary(url) {
  const res = await axios.get(url, {
    responseType: "arraybuffer",
    headers: { "User-Agent": USER_AGENT },
    timeout: 20000,
    maxRedirects: 5,
  });
  return res.data;
}

async function fetchText(url) {
  const res = await axios.get(url, {
    responseType: "text",
    headers: { "User-Agent": USER_AGENT },
    timeout: 20000,
    maxRedirects: 5,
  });
  return res.data;
}

function extractAssetUrlsFromCss(cssText, cssBase) {
  const urls = new Set();
  const urlRe = /url\(\s*['"]?([^'")]+)['"]?\s*\)/g;
  let m;
  while ((m = urlRe.exec(cssText)) !== null) {
    const raw = m[1].trim();
    const abs = absoluteUrl(cssBase, raw);
    if (abs) urls.add(abs);
  }
  const importRe = /@import\s+['"]([^'"]+)['"]/g;
  while ((m = importRe.exec(cssText)) !== null) {
    const raw = m[1].trim();
    const abs = absoluteUrl(cssBase, raw);
    if (abs) urls.add(abs);
  }
  return Array.from(urls);
}

module.exports = async function handler(req, res) {
  const url = (req.query && req.query.url) || (req.body && req.body.url);
  if (!url) {
    res.statusCode = 400;
    return res.json({ error: "Missing url query parameter. Use /api/clone?url=https://example.com" });
  }

  if (!/^https?:\/\//i.test(url)) {
    res.statusCode = 400;
    return res.json({ error: "URL must start with http:// or https://" });
  }

  try {
    const html = await fetchText(url);
    const $ = cheerio.load(html);

    const assetMap = new Map();
    let counter = 0;
    const limit = pLimit(6);
    const candidates = new Set();

    $("img[src], script[src], link[rel='stylesheet'][href], source[src], video[src], audio[src], iframe[src], link[rel~='icon'][href]").each((i, el) => {
      const attrib = el.attribs.src || el.attribs.href;
      if (attrib) {
        const abs = absoluteUrl(url, attrib);
        if (abs) candidates.add(abs);
      }
    });

    $("[style]").each((i, el) => {
      const style = el.attribs.style || "";
      const urlRegex = /url\(\s*['"]?([^'")]+)['"]?\s*\)/g;
      let m;
      while ((m = urlRegex.exec(style)) !== null) {
        const abs = absoluteUrl(url, m[1]);
        if (abs) candidates.add(abs);
      }
    });

    const cssLinks = [];
    $("link[rel='stylesheet'][href]").each((i, el) => {
      const href = el.attribs.href;
      const abs = absoluteUrl(url, href);
      if (abs) cssLinks.push(abs);
    });

    async function downloadAndStore(remoteUrl) {
      if (assetMap.has(remoteUrl)) return assetMap.get(remoteUrl).name;
      counter += 1;
      const name = sanitizeFilename(remoteUrl, counter);
      try {
        const data = await fetchBinary(remoteUrl);
        assetMap.set(remoteUrl, { name, data });
        return name;
      } catch (err) {
        assetMap.set(remoteUrl, { name, data: null, error: err.message });
        return name;
      }
    }

    const downloadPrimary = Array.from(candidates).map((u) => limit(() => downloadAndStore(u)));
    await Promise.all(downloadPrimary);

    for (const cssUrl of cssLinks) {
      try {
        const cssText = await fetchText(cssUrl);
        const cssName = await downloadAndStore(cssUrl);
        const cssAsset = assetMap.get(cssUrl);
        cssAsset.originalText = cssText;
        const cssChildUrls = extractAssetUrlsFromCss(cssText, cssUrl);
        const cssChildProm = cssChildUrls.map((u) => limit(() => downloadAndStore(u)));
        await Promise.all(cssChildProm);
      } catch (err) {
        await downloadAndStore(cssUrl).catch(()=>{});
      }
    }

    // Rewrite HTML asset links
    $("img[src], script[src], source[src], video[src], audio[src], iframe[src]").each((i, el) => {
      const attrib = el.attribs.src;
      if (attrib) {
        const abs = absoluteUrl(url, attrib);
        if (abs && assetMap.has(abs) && assetMap.get(abs).name) {
          $(el).attr("src", `assets/${assetMap.get(abs).name}`);
        }
      }
    });

    $("link[href]").each((i, el) => {
      const rel = (el.attribs.rel || "").toLowerCase();
      const href = el.attribs.href;
      if (!href) return;
      const abs = absoluteUrl(url, href);
      if (abs && assetMap.has(abs) && assetMap.get(abs).name) {
        $(el).attr("href", `assets/${assetMap.get(abs).name}`);
      }
    });

    $("[style]").each((i, el) => {
      let style = el.attribs.style || "";
      const urlRegex = /url\(\s*['"]?([^'")]+)['"]?\s*\)/g;
      style = style.replace(urlRegex, (m0, m1) => {
        const abs = absoluteUrl(url, m1);
        if (abs && assetMap.has(abs) && assetMap.get(abs).name) {
          return `url('assets/${assetMap.get(abs).name}')`;
        } else {
          return m0;
        }
      });
      $(el).attr("style", style);
    });

    // Rewrite CSS contents & store rewritten
    for (const [remoteUrl, info] of assetMap.entries()) {
      const isCss = /\.css($|\?)/i.test(remoteUrl) || (info.originalText && remoteUrl.toLowerCase().endsWith(".css"));
      if (isCss && info.originalText != null) {
        let cssText = info.originalText;
        const urlRegex = /url\(\s*(['"])?([^'")]+)\1\s*\)/g;
        cssText = cssText.replace(urlRegex, (m0, quote, p1) => {
          const abs = absoluteUrl(remoteUrl, p1);
          if (abs && assetMap.has(abs) && assetMap.get(abs).name) {
            return `url('assets/${assetMap.get(abs).name}')`;
          }
          return m0;
        });
        const importRe = /@import\s+(['"])([^'"]+)\1/g;
        cssText = cssText.replace(importRe, (m0, q, p1) => {
          const abs = absoluteUrl(remoteUrl, p1);
          if (abs && assetMap.has(abs) && assetMap.get(abs).name) {
            return `@import 'assets/${assetMap.get(abs).name}'`;
          }
          return m0;
        });
        info.data = Buffer.from(cssText, "utf-8");
        if (!info.name.endsWith(".css")) info.name += ".css";
        assetMap.set(remoteUrl, info);
      }
    }

    // Create ZIP
    const zip = new JSZip();
    const assetsFolder = zip.folder("assets");
    for (const [remoteUrl, info] of assetMap.entries()) {
      if (!info || !info.name) continue;
      if (info.data === null) {
        assetsFolder.file(`${info.name}.FAILED.txt`, `Failed to download ${remoteUrl}\nError: ${info.error || "unknown"}`);
        continue;
      }
      assetsFolder.file(info.name, info.data);
    }

    const finalHtml = $.html();
    zip.file("index.html", finalHtml);
    const zipBuf = await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE", compressionOptions: { level: 6 } });

    const safeName = (new URL(url)).hostname.replace(/[^a-zA-Z0-9.-]/g, "_");
    const filename = `${safeName}_clone.zip`;

    res.setHeader("Content-Type", "application/zip");
    res.setHeader("Content-Disposition", `attachment; filename="${filename}"`);
    res.statusCode = 200;
    res.end(zipBuf);

  } catch (err) {
    console.error("Clone error:", err && (err.stack || err.message));
    res.statusCode = 500;
    res.json({ error: "Failed to clone site", details: err.message || String(err) });
  }
};