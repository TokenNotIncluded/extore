use crate::common::*;
use image::{ImageFormat, ImageReader, Limits};
use std::io::Cursor;
fn invalid() -> ApiError {
    ApiError::bad("图片必须是可解码的 PNG、JPEG 或 WebP 静态图片，且不超过 1600 万像素")
}
pub fn content_type(bytes: &[u8]) -> Result<&'static str> {
    let format = image::guess_format(bytes).map_err(|_| invalid())?;
    let mime = match format {
        ImageFormat::Png => "image/png",
        ImageFormat::Jpeg => "image/jpeg",
        ImageFormat::WebP => "image/webp",
        _ => return Err(invalid()),
    };
    match format {
        ImageFormat::Png => {
            if !bytes.ends_with(b"\x00\x00\x00\x00IEND\xaeB\x60\x82") {
                return Err(invalid());
            }
            let mut i = 8usize;
            while i < bytes.len() {
                if bytes.len() - i < 12 {
                    return Err(invalid());
                }
                let n = u32::from_be_bytes(bytes[i..i + 4].try_into().unwrap()) as usize;
                let end = i
                    .checked_add(n)
                    .and_then(|v| v.checked_add(12))
                    .ok_or_else(invalid)?;
                if end > bytes.len() || &bytes[i + 4..i + 8] == b"acTL" {
                    return Err(invalid());
                }
                i = end;
            }
        }
        ImageFormat::Jpeg => {
            if !bytes.ends_with(b"\xff\xd9") {
                return Err(invalid());
            }
        }
        ImageFormat::WebP => {
            if bytes.len() < 12
                || u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize + 8 != bytes.len()
            {
                return Err(invalid());
            }
            let mut i = 12usize;
            let mut count = 0;
            while i < bytes.len() {
                count += 1;
                if count > 1024 || bytes.len() - i < 8 {
                    return Err(invalid());
                }
                let kind = &bytes[i..i + 4];
                let n = u32::from_le_bytes(bytes[i + 4..i + 8].try_into().unwrap()) as usize;
                let end = i
                    .checked_add(8)
                    .and_then(|v| v.checked_add(n))
                    .and_then(|v| v.checked_add(n & 1))
                    .ok_or_else(invalid)?;
                if end > bytes.len() || kind == b"ANIM" || kind == b"ANMF" {
                    return Err(invalid());
                }
                if kind == b"VP8X" && (n != 10 || bytes[i + 8] & 2 != 0) {
                    return Err(invalid());
                }
                i = end;
            }
        }
        _ => unreachable!(),
    }
    let (width, height) = ImageReader::with_format(Cursor::new(bytes), format)
        .into_dimensions()
        .map_err(|_| invalid())?;
    if width == 0 || height == 0 || u64::from(width) * u64::from(height) > 16_000_000 {
        return Err(invalid());
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(width);
    limits.max_image_height = Some(height);
    limits.max_alloc = Some(128 * 1024 * 1024);
    reader.limits(limits);
    reader.decode().map_err(|_| invalid())?;
    Ok(mime)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decodes_static_png_but_rejects_truncation_and_text() {
        let mut out = Cursor::new(Vec::new());
        image::DynamicImage::new_rgba8(2, 2)
            .write_to(&mut out, ImageFormat::Png)
            .unwrap();
        assert_eq!(content_type(out.get_ref()).unwrap(), "image/png");
        let mut v = out.into_inner();
        v.pop();
        assert!(content_type(&v).is_err());
        assert!(content_type(b"<svg onload=alert(1)>").is_err());
    }
}
