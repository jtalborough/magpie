(function () {
  "use strict";

  const DEFAULT_MAX_EDGE = 2048;
  const DEFAULT_JPEG_QUALITY = 0.82;
  const NORMALIZE_ABOVE_BYTES = 1.5 * 1024 * 1024;
  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

  function dataUrlBytes(dataUrl) {
    const encoded = String(dataUrl || "").split(",", 2).pop() || "";
    return Math.max(0, Math.floor(encoded.length * 3 / 4) - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0));
  }

  function readDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("Could not read photo"));
      reader.readAsDataURL(file);
    });
  }

  function contentType(file) {
    const name = String(file?.name || "");
    if (file?.type) return file.type;
    if (/\.heic$/i.test(name)) return "image/heic";
    if (/\.heif$/i.test(name)) return "image/heif";
    if (/\.png$/i.test(name)) return "image/png";
    if (/\.webp$/i.test(name)) return "image/webp";
    if (/\.gif$/i.test(name)) return "image/gif";
    return "image/jpeg";
  }

  function jpegFilename(name) {
    const clean = String(name || "photo.jpg");
    return /\.[a-z0-9]+$/i.test(clean) ? clean.replace(/\.[a-z0-9]+$/i, ".jpg") : `${clean}.jpg`;
  }

  function decodeImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      const finish = () => URL.revokeObjectURL(url);
      image.onload = () => {
        finish();
        resolve(image);
      };
      image.onerror = () => {
        finish();
        reject(new Error("This browser cannot decode the selected photo"));
      };
      image.src = url;
    });
  }

  async function prepare(file, options = {}) {
    const maxEdge = Math.max(320, Number(options.maxEdge || DEFAULT_MAX_EDGE));
    const quality = Math.min(0.95, Math.max(0.5, Number(options.quality || DEFAULT_JPEG_QUALITY)));
    const originalName = file?.name || "photo.jpg";
    const originalType = contentType(file);
    const sourceBytes = Number(file?.size || 0);
    const preserveFormat = /image\/(?:gif|svg\+xml)/i.test(originalType);
    let image = null;

    if (!preserveFormat) {
      try {
        image = await decodeImage(file);
      } catch (error) {
        if (sourceBytes > MAX_UPLOAD_BYTES) {
          throw new Error("This photo is over 10 MB and this browser cannot resize it");
        }
      }
    }

    const width = Number(image?.naturalWidth || image?.width || 0);
    const height = Number(image?.naturalHeight || image?.height || 0);
    const largestEdge = Math.max(width, height);
    const isHeic = /image\/hei[cf]/i.test(originalType) || /\.(?:heic|heif)$/i.test(originalName);
    const shouldNormalize = Boolean(image) && (isHeic || largestEdge > maxEdge || sourceBytes > NORMALIZE_ABOVE_BYTES);

    if (shouldNormalize) {
      const scale = largestEdge > maxEdge ? maxEdge / largestEdge : 1;
      const outputWidth = Math.max(1, Math.round(width * scale));
      const outputHeight = Math.max(1, Math.round(height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = outputWidth;
      canvas.height = outputHeight;
      const context = canvas.getContext("2d", {alpha:false});
      if (!context) throw new Error("This browser cannot prepare the selected photo");
      context.drawImage(image, 0, 0, outputWidth, outputHeight);
      const normalizedDataUrl = canvas.toDataURL("image/jpeg", quality);
      const normalizedBytes = dataUrlBytes(normalizedDataUrl);
      const reducedDimensions = outputWidth < width || outputHeight < height;
      if (reducedDimensions || isHeic || !sourceBytes || normalizedBytes < sourceBytes) {
        return {
          filename:jpegFilename(originalName),
          contentType:"image/jpeg",
          dataUrl:normalizedDataUrl,
          width:outputWidth,
          height:outputHeight,
          sourceBytes,
          uploadBytes:normalizedBytes,
          normalized:true
        };
      }
    }

    const dataUrl = await readDataUrl(file);
    const uploadBytes = dataUrlBytes(dataUrl);
    if (uploadBytes > MAX_UPLOAD_BYTES) throw new Error("Photo must be 10 MB or smaller");
    return {
      filename:originalName,
      contentType:originalType,
      dataUrl,
      width,
      height,
      sourceBytes,
      uploadBytes,
      normalized:false
    };
  }

  window.magpiePhotos = Object.freeze({
    prepare,
    dataUrlBytes,
    maxEdge:DEFAULT_MAX_EDGE,
    maxUploadBytes:MAX_UPLOAD_BYTES
  });
})();
