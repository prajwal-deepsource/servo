/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

//! Servo's media-query device and expression representation.

use crate::context::QuirksMode;
use crate::custom_properties::CssEnvironment;
use crate::font_metrics::FontMetrics;
use crate::media_queries::media_feature::{AllowsRanges, ParsingRequirements};
use crate::media_queries::media_feature::{Evaluator, MediaFeatureDescription};
use crate::media_queries::media_feature_expression::RangeOrOperator;
use crate::media_queries::MediaType;
use crate::properties::ComputedValues;
use crate::values::computed::CSSPixelLength;
use crate::values::specified::font::FONT_MEDIUM_PX;
use crate::values::KeyframesName;
use app_units::Au;
use cssparser::RGBA;
use euclid::default::Size2D as UntypedSize2D;
use euclid::{Scale, SideOffsets2D, Size2D};
use mime::Mime;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use style_traits::viewport::ViewportConstraints;
use style_traits::{CSSPixel, DevicePixel};

/// A device is a structure that represents the current media a given document
/// is displayed in.
///
/// This is the struct against which media queries are evaluated.
#[derive(Debug, MallocSizeOf)]
pub struct Device {
    /// The current media type used by de device.
    media_type: MediaType,
    /// The current viewport size, in CSS pixels.
    viewport_size: Size2D<f32, CSSPixel>,
    /// The current device pixel ratio, from CSS pixels to device pixels.
    device_pixel_ratio: Scale<f32, CSSPixel, DevicePixel>,
    /// The current quirks mode.
    #[ignore_malloc_size_of = "Pure stack type"]
    quirks_mode: QuirksMode,

    /// The font size of the root element
    /// This is set when computing the style of the root
    /// element, and used for rem units in other elements
    ///
    /// When computing the style of the root element, there can't be any
    /// other style being computed at the same time, given we need the style of
    /// the parent to compute everything else. So it is correct to just use
    /// a relaxed atomic here.
    #[ignore_malloc_size_of = "Pure stack type"]
    root_font_size: AtomicU32,
    /// Whether any styles computed in the document relied on the root font-size
    /// by using rem units.
    #[ignore_malloc_size_of = "Pure stack type"]
    used_root_font_size: AtomicBool,
    /// Whether any styles computed in the document relied on the viewport size.
    #[ignore_malloc_size_of = "Pure stack type"]
    used_viewport_units: AtomicBool,
    /// The CssEnvironment object responsible of getting CSS environment
    /// variables.
    environment: CssEnvironment,
}

impl Device {
    /// Trivially construct a new `Device`.
    pub fn new(
        media_type: MediaType,
        quirks_mode: QuirksMode,
        viewport_size: Size2D<f32, CSSPixel>,
        device_pixel_ratio: Scale<f32, CSSPixel, DevicePixel>,
    ) -> Device {
        Device {
            media_type,
            viewport_size,
            device_pixel_ratio,
            quirks_mode,
            // FIXME(bz): Seems dubious?
            root_font_size: AtomicU32::new(FONT_MEDIUM_PX.to_bits()),
            used_root_font_size: AtomicBool::new(false),
            used_viewport_units: AtomicBool::new(false),
            environment: CssEnvironment,
        }
    }

    /// Get the relevant environment to resolve `env()` functions.
    #[inline]
    pub fn environment(&self) -> &CssEnvironment {
        &self.environment
    }

    /// Return the default computed values for this device.
    pub fn default_computed_values(&self) -> &ComputedValues {
        // FIXME(bz): This isn't really right, but it's no more wrong
        // than what we used to do.  See
        // https://github.com/servo/servo/issues/14773 for fixing it properly.
        ComputedValues::initial_values()
    }

    /// Get the font size of the root element (for rem)
    pub fn root_font_size(&self) -> CSSPixelLength {
        self.used_root_font_size.store(true, Ordering::Relaxed);
        CSSPixelLength::new(f32::from_bits(self.root_font_size.load(Ordering::Relaxed)))
    }

    /// Set the font size of the root element (for rem)
    pub fn set_root_font_size(&self, size: CSSPixelLength) {
        self.root_font_size
            .store(size.px().to_bits(), Ordering::Relaxed)
    }

    /// Get the quirks mode of the current device.
    pub fn quirks_mode(&self) -> QuirksMode {
        self.quirks_mode
    }

    /// Sets the body text color for the "inherit color from body" quirk.
    ///
    /// <https://quirks.spec.whatwg.org/#the-tables-inherit-color-from-body-quirk>
    pub fn set_body_text_color(&self, _color: RGBA) {
        // Servo doesn't implement this quirk (yet)
    }

    /// Whether a given animation name may be referenced from style.
    pub fn animation_name_may_be_referenced(&self, _: &KeyframesName) -> bool {
        // Assume it is, since we don't have any good way to prove it's not.
        true
    }

    /// Returns whether we ever looked up the root font size of the Device.
    pub fn used_root_font_size(&self) -> bool {
        self.used_root_font_size.load(Ordering::Relaxed)
    }

    /// Returns the viewport size of the current device in app units, needed,
    /// among other things, to resolve viewport units.
    #[inline]
    pub fn au_viewport_size(&self) -> UntypedSize2D<Au> {
        Size2D::new(
            Au::from_f32_px(self.viewport_size.width),
            Au::from_f32_px(self.viewport_size.height),
        )
    }

    /// Like the above, but records that we've used viewport units.
    pub fn au_viewport_size_for_viewport_unit_resolution(&self) -> UntypedSize2D<Au> {
        self.used_viewport_units.store(true, Ordering::Relaxed);
        self.au_viewport_size()
    }

    /// Whether viewport units were used since the last device change.
    pub fn used_viewport_units(&self) -> bool {
        self.used_viewport_units.load(Ordering::Relaxed)
    }

    /// Returns the device pixel ratio.
    pub fn device_pixel_ratio(&self) -> Scale<f32, CSSPixel, DevicePixel> {
        self.device_pixel_ratio
    }

    /// Queries dummy font metrics for Servo. Knows nothing about fonts and does not provide
    /// any metrics.
    /// TODO: Servo's font metrics provider will probably not live in this crate, so this will
    /// have to be replaced with something else (perhaps a trait method on TElement)
    /// when we get there
    pub fn query_font_metrics(
        &self,
        _vertical: bool,
        _font: &crate::properties::style_structs::Font,
        _base_size: CSSPixelLength,
        _in_media_query: bool,
        _retrieve_math_scales: bool,
    ) -> FontMetrics {
        Default::default()
    }

    /// Take into account a viewport rule taken from the stylesheets.
    pub fn account_for_viewport_rule(&mut self, constraints: &ViewportConstraints) {
        self.viewport_size = constraints.size;
    }

    /// Return the media type of the current device.
    pub fn media_type(&self) -> MediaType {
        self.media_type.clone()
    }

    /// Returns whether document colors are enabled.
    pub fn use_document_colors(&self) -> bool {
        true
    }

    /// Returns the default background color.
    pub fn default_background_color(&self) -> RGBA {
        RGBA::new(255, 255, 255, 255)
    }

    /// Returns the default color color.
    pub fn default_color(&self) -> RGBA {
        RGBA::new(0, 0, 0, 255)
    }

    /// Returns safe area insets
    pub fn safe_area_insets(&self) -> SideOffsets2D<f32, CSSPixel> {
        SideOffsets2D::zero()
    }

    /// Returns true if the given MIME type is supported
    pub fn is_supported_mime_type(&self, mime_type: &str) -> bool {
        match mime_type.parse::<Mime>() {
            Ok(m) => {
                // Keep this in sync with 'image_classifer' from
                // components/net/mime_classifier.rs
                m == mime::IMAGE_BMP
                    || m == mime::IMAGE_GIF
                    || m == mime::IMAGE_PNG
                    || m == mime::IMAGE_JPEG
                    || m == "image/x-icon"
                    || m == "image/webp"
            }
            _ => false,
        }
    }
}

/// https://drafts.csswg.org/mediaqueries-4/#width
fn eval_width(
    device: &Device,
    value: Option<CSSPixelLength>,
    range_or_operator: Option<RangeOrOperator>,
) -> bool {
    RangeOrOperator::evaluate(
        range_or_operator,
        value.map(Au::from),
        device.au_viewport_size().width,
    )
}

#[derive(Clone, Copy, Debug, FromPrimitive, Parse, ToCss)]
#[repr(u8)]
enum Scan {
    Progressive,
    Interlace,
}

/// https://drafts.csswg.org/mediaqueries-4/#scan
fn eval_scan(_: &Device, _: Option<Scan>) -> bool {
    // Since we doesn't support the 'tv' media type, the 'scan' feature never
    // matches.
    false
}

lazy_static! {
    /// A list with all the media features that Servo supports.
    pub static ref MEDIA_FEATURES: [MediaFeatureDescription; 2] = [
        feature!(
            atom!("width"),
            AllowsRanges::Yes,
            Evaluator::Length(eval_width),
            ParsingRequirements::empty(),
        ),
        feature!(
            atom!("scan"),
            AllowsRanges::No,
            keyword_evaluator!(eval_scan, Scan),
            ParsingRequirements::empty(),
        ),
    ];
}
