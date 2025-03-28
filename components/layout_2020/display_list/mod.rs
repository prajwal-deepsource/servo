/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/. */

use crate::context::LayoutContext;
use crate::display_list::conversions::ToWebRender;
use crate::display_list::stacking_context::StackingContextSection;
use crate::fragment_tree::Tag;
use crate::fragments::{BoxFragment, Fragment, TextFragment};
use crate::geom::{PhysicalPoint, PhysicalRect};
use crate::replaced::IntrinsicSizes;
use crate::style_ext::ComputedValuesExt;
use crate::FragmentTree;
use embedder_traits::Cursor;
use euclid::{Point2D, SideOffsets2D, Size2D};
use fnv::FnvHashMap;
use gfx::text::glyph::GlyphStore;
use mitochondria::OnceCell;
use msg::constellation_msg::BrowsingContextId;
use net_traits::image_cache::UsePlaceholder;
use script_traits::compositor::{CompositorDisplayListInfo, ScrollTreeNodeId};
use std::sync::Arc;
use style::computed_values::text_decoration_style::T as ComputedTextDecorationStyle;
use style::dom::OpaqueNode;
use style::properties::longhands::visibility::computed_value::T as Visibility;
use style::properties::ComputedValues;
use style::values::computed::{BorderStyle, Color, Length, LengthPercentage, OutlineStyle};
use style::values::specified::text::TextDecorationLine;
use style::values::specified::ui::CursorKind;
use style_traits::CSSPixel;
use webrender_api::{self as wr, units};

mod background;
mod conversions;
mod gradient;
mod stacking_context;

pub use stacking_context::*;

#[derive(Clone, Copy)]
pub struct WebRenderImageInfo {
    pub width: u32,
    pub height: u32,
    pub key: Option<wr::ImageKey>,
}

// `webrender_api::display_item::ItemTag` is private
type ItemTag = (u64, u16);
type HitInfo = Option<ItemTag>;

/// Where the information that's used to build display lists is stored. This
/// includes both a [wr::DisplayListBuilder] for building up WebRender-specific
/// display list information and a [CompositorDisplayListInfo] used to store
/// information used by the compositor, such as a compositor-side scroll tree.
pub struct DisplayList {
    /// The [wr::DisplayListBuilder] used to collect display list items.
    pub wr: wr::DisplayListBuilder,

    /// The information about the WebRender display list that the compositor
    /// consumes. This curerntly contains the out-of-band hit testing information
    /// data structure that the compositor uses to map hit tests to information
    /// about the item hit.
    pub compositor_info: CompositorDisplayListInfo,
}

impl DisplayList {
    /// Create a new [DisplayList] given the dimensions of the layout and the WebRender
    /// pipeline id.
    pub fn new(
        viewport_size: units::LayoutSize,
        content_size: units::LayoutSize,
        pipeline_id: wr::PipelineId,
        epoch: wr::Epoch,
    ) -> Self {
        Self {
            wr: wr::DisplayListBuilder::new(pipeline_id, content_size),
            compositor_info: CompositorDisplayListInfo::new(
                viewport_size,
                content_size,
                pipeline_id,
                epoch,
            ),
        }
    }
}

pub(crate) struct DisplayListBuilder<'a> {
    /// The current [ScrollTreeNodeId] for this [DisplayListBuilder]. This
    /// allows only passing the builder instead passing the containing
    /// [stacking_context::StackingContextFragment] as an argument to display
    /// list building functions.
    current_scroll_node_id: ScrollTreeNodeId,

    /// The current [wr::ClipId] for this [DisplayListBuilder]. This allows
    /// only passing the builder instead passing the containing
    /// [stacking_context::StackingContextFragment] as an argument to display
    /// list building functions.
    current_clip_id: wr::ClipId,

    /// The [OpaqueNode] handle to the node used to paint the page background
    /// if the background was a canvas.
    element_for_canvas_background: OpaqueNode,

    /// A [LayoutContext] used to get information about the device pixel ratio
    /// and get handles to WebRender images.
    pub context: &'a LayoutContext<'a>,

    /// The [DisplayList] used to collect display list items and metadata.
    pub display_list: &'a mut DisplayList,

    /// A recording of the sizes of iframes encountered when building this
    /// display list. This information is forwarded to the layout thread for the
    /// iframe so that its layout knows how large the initial containing block /
    /// viewport is.
    iframe_sizes: FnvHashMap<BrowsingContextId, Size2D<f32, CSSPixel>>,

    /// Contentful paint i.e. whether the display list contains items of type
    /// text, image, non-white canvas or SVG). Used by metrics.
    /// See https://w3c.github.io/paint-timing/#first-contentful-paint.
    is_contentful: bool,
}

impl DisplayList {
    pub fn build<'a>(
        &mut self,
        context: &'a LayoutContext,
        fragment_tree: &FragmentTree,
        root_stacking_context: &StackingContext,
    ) -> (FnvHashMap<BrowsingContextId, Size2D<f32, CSSPixel>>, bool) {
        let mut builder = DisplayListBuilder {
            current_scroll_node_id: self.compositor_info.root_scroll_node_id,
            current_clip_id: wr::ClipId::root(self.wr.pipeline_id),
            element_for_canvas_background: fragment_tree.canvas_background.from_element,
            is_contentful: false,
            context,
            display_list: self,
            iframe_sizes: FnvHashMap::default(),
        };
        fragment_tree.build_display_list(&mut builder, root_stacking_context);
        (builder.iframe_sizes, builder.is_contentful)
    }
}

impl<'a> DisplayListBuilder<'a> {
    fn wr(&mut self) -> &mut wr::DisplayListBuilder {
        &mut self.display_list.wr
    }

    fn common_properties(
        &self,
        clip_rect: units::LayoutRect,
        style: &ComputedValues,
    ) -> wr::CommonItemProperties {
        // TODO(mrobinson): We should take advantage of this field to pass hit testing
        // information. This will allow us to avoid creating hit testing display items
        // for fragments that paint their entire border rectangle.
        wr::CommonItemProperties {
            clip_rect,
            spatial_id: self.current_scroll_node_id.spatial_id,
            clip_id: self.current_clip_id,
            hit_info: None,
            flags: style.get_webrender_primitive_flags(),
        }
    }

    fn hit_info(
        &mut self,
        style: &ComputedValues,
        tag: Option<Tag>,
        auto_cursor: Cursor,
    ) -> HitInfo {
        use style::computed_values::pointer_events::T as PointerEvents;

        let inherited_ui = style.get_inherited_ui();
        if inherited_ui.pointer_events == PointerEvents::None {
            return None;
        }

        let hit_test_index = self.display_list.compositor_info.add_hit_test_info(
            tag?.node.0 as u64,
            Some(cursor(inherited_ui.cursor.keyword, auto_cursor)),
            self.current_scroll_node_id,
        );
        Some((hit_test_index as u64, 0u16))
    }
}

impl Fragment {
    pub(crate) fn build_display_list(
        &self,
        builder: &mut DisplayListBuilder,
        containing_block: &PhysicalRect<Length>,
        section: StackingContextSection,
    ) {
        match self {
            Fragment::Box(b) => match b.style.get_inherited_box().visibility {
                Visibility::Visible => {
                    BuilderForBoxFragment::new(b, containing_block).build(builder, section)
                },
                Visibility::Hidden => (),
                Visibility::Collapse => (),
            },
            Fragment::AbsoluteOrFixedPositioned(_) => {},
            Fragment::Anonymous(_) => {},
            Fragment::Image(i) => match i.style.get_inherited_box().visibility {
                Visibility::Visible => {
                    builder.is_contentful = true;
                    let rect = i
                        .rect
                        .to_physical(i.style.writing_mode, containing_block)
                        .translate(containing_block.origin.to_vector());

                    let common = builder.common_properties(rect.to_webrender(), &i.style);
                    builder.wr().push_image(
                        &common,
                        rect.to_webrender(),
                        image_rendering(i.style.get_inherited_box().image_rendering),
                        wr::AlphaType::PremultipliedAlpha,
                        i.image_key,
                        wr::ColorF::WHITE,
                    );
                },
                Visibility::Hidden => (),
                Visibility::Collapse => (),
            },
            Fragment::IFrame(iframe) => match iframe.style.get_inherited_box().visibility {
                Visibility::Visible => {
                    builder.is_contentful = true;
                    let rect = iframe
                        .rect
                        .to_physical(iframe.style.writing_mode, containing_block)
                        .translate(containing_block.origin.to_vector());

                    builder.iframe_sizes.insert(
                        iframe.browsing_context_id,
                        Size2D::new(rect.size.width.px(), rect.size.height.px()),
                    );

                    let common = builder.common_properties(rect.to_webrender(), &iframe.style);
                    builder.wr().push_iframe(
                        rect.to_webrender(),
                        common.clip_rect,
                        &wr::SpaceAndClipInfo {
                            spatial_id: common.spatial_id,
                            clip_id: common.clip_id,
                        },
                        iframe.pipeline_id.to_webrender(),
                        true,
                    );
                },
                Visibility::Hidden => (),
                Visibility::Collapse => (),
            },
            Fragment::Text(t) => match t.parent_style.get_inherited_box().visibility {
                Visibility::Visible => {
                    self.build_display_list_for_text_fragment(t, builder, containing_block)
                },
                Visibility::Hidden => (),
                Visibility::Collapse => (),
            },
        }
    }

    fn build_display_list_for_text_fragment(
        &self,
        fragment: &TextFragment,
        builder: &mut DisplayListBuilder,
        containing_block: &PhysicalRect<Length>,
    ) {
        // NB: The order of painting text components (CSS Text Decoration Module Level 3) is:
        // shadows, underline, overline, text, text-emphasis, and then line-through.

        builder.is_contentful = true;

        let rect = fragment
            .rect
            .to_physical(fragment.parent_style.writing_mode, containing_block)
            .translate(containing_block.origin.to_vector());
        let mut baseline_origin = rect.origin.clone();
        baseline_origin.y += fragment.font_metrics.ascent;
        let glyphs = glyphs(&fragment.glyphs, baseline_origin);
        if glyphs.is_empty() {
            return;
        }

        let mut common = builder.common_properties(rect.to_webrender(), &fragment.parent_style);
        common.hit_info = builder.hit_info(&fragment.parent_style, fragment.base.tag, Cursor::Text);

        let color = fragment.parent_style.clone_color();
        let font_metrics = &fragment.font_metrics;
        let dppx = builder.context.style_context.device_pixel_ratio().get();
        let round_to_nearest_device_pixel = |value: Length| -> Length {
            // Round to the nearest integer device pixel, ensuring at least one device pixel.
            Length::new((value.px() * dppx).round().max(1.0) / dppx)
        };

        // Underline.
        if fragment
            .text_decoration_line
            .contains(TextDecorationLine::UNDERLINE)
        {
            let mut rect = rect;
            rect.origin.y = rect.origin.y + font_metrics.ascent - font_metrics.underline_offset;
            rect.size.height = round_to_nearest_device_pixel(font_metrics.underline_size);
            self.build_display_list_for_text_decoration(fragment, builder, &rect, color);
        }

        // Overline.
        if fragment
            .text_decoration_line
            .contains(TextDecorationLine::OVERLINE)
        {
            let mut rect = rect;
            rect.size.height = round_to_nearest_device_pixel(font_metrics.underline_size);
            self.build_display_list_for_text_decoration(fragment, builder, &rect, color);
        }

        // Text.
        builder.wr().push_text(
            &common,
            rect.to_webrender(),
            &glyphs,
            fragment.font_key,
            rgba(color),
            None,
        );

        // Line-through.
        if fragment
            .text_decoration_line
            .contains(TextDecorationLine::LINE_THROUGH)
        {
            let mut rect = rect;
            rect.origin.y = rect.origin.y + font_metrics.ascent - font_metrics.strikeout_offset;
            // XXX(ferjm) This does not work on MacOS #942
            rect.size.height = round_to_nearest_device_pixel(font_metrics.strikeout_size);
            self.build_display_list_for_text_decoration(fragment, builder, &rect, color);
        }
    }

    fn build_display_list_for_text_decoration(
        &self,
        fragment: &TextFragment,
        builder: &mut DisplayListBuilder,
        rect: &PhysicalRect<Length>,
        color: cssparser::RGBA,
    ) {
        let rect = rect.to_webrender();
        let wavy_line_thickness = (0.33 * rect.size.height).ceil();
        let text_decoration_color = fragment
            .parent_style
            .clone_text_decoration_color()
            .to_rgba(color);
        let text_decoration_style = fragment.parent_style.clone_text_decoration_style();
        if text_decoration_style == ComputedTextDecorationStyle::MozNone {
            return;
        }
        builder.display_list.wr.push_line(
            &builder.common_properties(rect, &fragment.parent_style),
            &rect,
            wavy_line_thickness,
            wr::LineOrientation::Horizontal,
            &rgba(text_decoration_color),
            text_decoration_style.to_webrender(),
        );
        // XXX(ferjm) support text-decoration-style: double
    }
}

struct BuilderForBoxFragment<'a> {
    fragment: &'a BoxFragment,
    containing_block: &'a PhysicalRect<Length>,
    border_rect: units::LayoutRect,
    padding_rect: OnceCell<units::LayoutRect>,
    content_rect: OnceCell<units::LayoutRect>,
    border_radius: wr::BorderRadius,
    border_edge_clip_id: OnceCell<Option<wr::ClipId>>,
    padding_edge_clip_id: OnceCell<Option<wr::ClipId>>,
    content_edge_clip_id: OnceCell<Option<wr::ClipId>>,
}

impl<'a> BuilderForBoxFragment<'a> {
    fn new(fragment: &'a BoxFragment, containing_block: &'a PhysicalRect<Length>) -> Self {
        let border_rect: units::LayoutRect = fragment
            .border_rect()
            .to_physical(fragment.style.writing_mode, containing_block)
            .translate(containing_block.origin.to_vector())
            .to_webrender();

        let border_radius = {
            let resolve = |radius: &LengthPercentage, box_size: f32| {
                radius.percentage_relative_to(Length::new(box_size)).px()
            };
            let corner = |corner: &style::values::computed::BorderCornerRadius| {
                Size2D::new(
                    resolve(&corner.0.width.0, border_rect.size.width),
                    resolve(&corner.0.height.0, border_rect.size.height),
                )
            };
            let b = fragment.style.get_border();
            let mut radius = wr::BorderRadius {
                top_left: corner(&b.border_top_left_radius),
                top_right: corner(&b.border_top_right_radius),
                bottom_right: corner(&b.border_bottom_right_radius),
                bottom_left: corner(&b.border_bottom_left_radius),
            };
            // Normalize radii that add up to > 100%.
            // https://www.w3.org/TR/css-backgrounds-3/#corner-overlap
            // > Let f = min(L_i/S_i), where i ∈ {top, right, bottom, left},
            // > S_i is the sum of the two corresponding radii of the corners on side i,
            // > and L_top = L_bottom = the width of the box,
            // > and L_left = L_right = the height of the box.
            // > If f < 1, then all corner radii are reduced by multiplying them by f.
            let f = (border_rect.width() / (radius.top_left.width + radius.top_right.width))
                .min(border_rect.width() / (radius.bottom_left.width + radius.bottom_right.width))
                .min(border_rect.height() / (radius.top_left.height + radius.bottom_left.height))
                .min(border_rect.height() / (radius.top_right.height + radius.bottom_right.height));
            if f < 1.0 {
                radius.top_left *= f;
                radius.top_right *= f;
                radius.bottom_right *= f;
                radius.bottom_left *= f;
            }
            radius
        };

        Self {
            fragment,
            containing_block,
            border_rect,
            border_radius,
            padding_rect: OnceCell::new(),
            content_rect: OnceCell::new(),
            border_edge_clip_id: OnceCell::new(),
            padding_edge_clip_id: OnceCell::new(),
            content_edge_clip_id: OnceCell::new(),
        }
    }

    fn content_rect(&self) -> &units::LayoutRect {
        self.content_rect.init_once(|| {
            self.fragment
                .content_rect
                .to_physical(self.fragment.style.writing_mode, self.containing_block)
                .translate(self.containing_block.origin.to_vector())
                .to_webrender()
        })
    }

    fn padding_rect(&self) -> &units::LayoutRect {
        self.padding_rect.init_once(|| {
            self.fragment
                .padding_rect()
                .to_physical(self.fragment.style.writing_mode, self.containing_block)
                .translate(self.containing_block.origin.to_vector())
                .to_webrender()
        })
    }

    fn border_edge_clip(&self, builder: &mut DisplayListBuilder) -> Option<wr::ClipId> {
        *self
            .border_edge_clip_id
            .init_once(|| clip_for_radii(self.border_radius, self.border_rect, builder))
    }

    fn padding_edge_clip(&self, builder: &mut DisplayListBuilder) -> Option<wr::ClipId> {
        *self.padding_edge_clip_id.init_once(|| {
            clip_for_radii(
                inner_radii(
                    self.border_radius,
                    self.fragment
                        .border
                        .to_physical(self.fragment.style.writing_mode)
                        .to_webrender(),
                ),
                *self.padding_rect(),
                builder,
            )
        })
    }

    fn content_edge_clip(&self, builder: &mut DisplayListBuilder) -> Option<wr::ClipId> {
        *self.content_edge_clip_id.init_once(|| {
            clip_for_radii(
                inner_radii(
                    self.border_radius,
                    (&self.fragment.border + &self.fragment.padding)
                        .to_physical(self.fragment.style.writing_mode)
                        .to_webrender(),
                ),
                *self.content_rect(),
                builder,
            )
        })
    }

    fn build(&mut self, builder: &mut DisplayListBuilder, section: StackingContextSection) {
        if section == StackingContextSection::Outline {
            self.build_outline(builder);
        } else {
            self.build_hit_test(builder);
            self.build_background(builder);
            self.build_border(builder);
        }
    }

    fn build_hit_test(&self, builder: &mut DisplayListBuilder) {
        let hit_info = builder.hit_info(
            &self.fragment.style,
            self.fragment.base.tag,
            Cursor::Default,
        );
        if hit_info.is_some() {
            let mut common = builder.common_properties(self.border_rect, &self.fragment.style);
            common.hit_info = hit_info;
            if let Some(clip_id) = self.border_edge_clip(builder) {
                common.clip_id = clip_id
            }
            builder.wr().push_hit_test(&common)
        }
    }

    fn build_background(&mut self, builder: &mut DisplayListBuilder) {
        if self
            .fragment
            .base
            .is_for_node(builder.element_for_canvas_background)
        {
            // This background is already painted for the canvas, don’t paint it again here.
            return;
        }

        let source = background::Source::Fragment;
        let style = &self.fragment.style;
        let b = style.get_background();
        let background_color = style.resolve_color(b.background_color);
        if background_color.alpha > 0 {
            // https://drafts.csswg.org/css-backgrounds/#background-color
            // “The background color is clipped according to the background-clip
            //  value associated with the bottom-most background image layer.”
            let layer_index = b.background_image.0.len() - 1;
            let (bounds, common) = background::painting_area(self, &source, builder, layer_index);
            builder
                .wr()
                .push_rect(&common, *bounds, rgba(background_color))
        }

        self.build_background_image(builder, source);
    }

    fn build_background_image(
        &mut self,
        builder: &mut DisplayListBuilder,
        source: background::Source<'a>,
    ) {
        use style::values::computed::image::Image;
        let style = match source {
            background::Source::Canvas { style, .. } => style,
            background::Source::Fragment => &self.fragment.style,
        };
        let b = style.get_background();
        // Reverse because the property is top layer first, we want to paint bottom layer first.
        for (index, image) in b.background_image.0.iter().enumerate().rev() {
            match image {
                Image::None => {},
                Image::Gradient(ref gradient) => {
                    let intrinsic = IntrinsicSizes {
                        width: None,
                        height: None,
                        ratio: None,
                    };
                    if let Some(layer) =
                        &background::layout_layer(self, &source, builder, index, intrinsic)
                    {
                        gradient::build(&style, &gradient, layer, builder)
                    }
                },
                Image::Url(ref image_url) => {
                    // FIXME: images won’t always have in intrinsic width or
                    // height when support for SVG is added, or a WebRender
                    // `ImageKey`, for that matter.
                    //
                    // FIXME: It feels like this should take into account the pseudo
                    // element and not just the node.
                    let node = match self.fragment.base.tag {
                        Some(tag) => tag.node,
                        None => continue,
                    };
                    let image_url = match image_url.url() {
                        Some(url) => url.clone(),
                        None => continue,
                    };
                    let (width, height, key) = match builder.context.get_webrender_image_for_url(
                        node,
                        image_url,
                        UsePlaceholder::No,
                    ) {
                        Some(WebRenderImageInfo {
                            width,
                            height,
                            key: Some(key),
                        }) => (width, height, key),
                        _ => continue,
                    };

                    // FIXME: https://drafts.csswg.org/css-images-4/#the-image-resolution
                    let dppx = 1.0;

                    let intrinsic = IntrinsicSizes {
                        width: Some(Length::new(width as f32 / dppx)),
                        height: Some(Length::new(height as f32 / dppx)),
                        // FIXME https://github.com/w3c/csswg-drafts/issues/4572
                        ratio: Some(width as f32 / height as f32),
                    };

                    if let Some(layer) =
                        background::layout_layer(self, &source, builder, index, intrinsic)
                    {
                        let image_rendering = image_rendering(style.clone_image_rendering());
                        if layer.repeat {
                            builder.wr().push_repeating_image(
                                &layer.common,
                                layer.bounds,
                                layer.tile_size,
                                layer.tile_spacing,
                                image_rendering,
                                wr::AlphaType::PremultipliedAlpha,
                                key,
                                wr::ColorF::WHITE,
                            )
                        } else {
                            builder.wr().push_image(
                                &layer.common,
                                layer.bounds,
                                image_rendering,
                                wr::AlphaType::PremultipliedAlpha,
                                key,
                                wr::ColorF::WHITE,
                            )
                        }
                    }
                },
                // Gecko-only value, represented as a (boxed) empty enum on non-Gecko.
                Image::Rect(ref rect) => match **rect {},
                Image::ImageSet(..) | Image::CrossFade(..) => {
                    unreachable!("Shouldn't be parsed on Servo for now")
                },
            }
        }
    }

    fn build_border_side(&mut self, style: BorderStyle, color: Color) -> wr::BorderSide {
        wr::BorderSide {
            color: rgba(self.fragment.style.resolve_color(color)),
            style: match style {
                BorderStyle::None => wr::BorderStyle::None,
                BorderStyle::Solid => wr::BorderStyle::Solid,
                BorderStyle::Double => wr::BorderStyle::Double,
                BorderStyle::Dotted => wr::BorderStyle::Dotted,
                BorderStyle::Dashed => wr::BorderStyle::Dashed,
                BorderStyle::Hidden => wr::BorderStyle::Hidden,
                BorderStyle::Groove => wr::BorderStyle::Groove,
                BorderStyle::Ridge => wr::BorderStyle::Ridge,
                BorderStyle::Inset => wr::BorderStyle::Inset,
                BorderStyle::Outset => wr::BorderStyle::Outset,
            },
        }
    }

    fn build_border(&mut self, builder: &mut DisplayListBuilder) {
        let border = self.fragment.style.get_border();
        let widths = SideOffsets2D::new(
            border.border_top_width.px(),
            border.border_right_width.px(),
            border.border_bottom_width.px(),
            border.border_left_width.px(),
        );
        if widths == SideOffsets2D::zero() {
            return;
        }
        let common = builder.common_properties(self.border_rect, &self.fragment.style);
        let details = wr::BorderDetails::Normal(wr::NormalBorder {
            top: self.build_border_side(border.border_top_style, border.border_top_color),
            right: self.build_border_side(border.border_right_style, border.border_right_color),
            bottom: self.build_border_side(border.border_bottom_style, border.border_bottom_color),
            left: self.build_border_side(border.border_left_style, border.border_left_color),
            radius: self.border_radius,
            do_aa: true,
        });
        builder
            .wr()
            .push_border(&common, self.border_rect, widths, details)
    }

    fn build_outline(&mut self, builder: &mut DisplayListBuilder) {
        let outline = self.fragment.style.get_outline();
        let width = outline.outline_width.px();
        if width == 0.0 {
            return;
        }
        let offset = outline
            .outline_offset
            .px()
            .max(-self.border_rect.width() / 2.0)
            .max(-self.border_rect.height() / 2.0) +
            width;
        let outline_rect = self.border_rect.inflate(offset, offset);
        let common = builder.common_properties(outline_rect, &self.fragment.style);
        let widths = SideOffsets2D::new_all_same(width);
        let style = match outline.outline_style {
            // TODO: treating 'auto' as 'solid' is allowed by the spec,
            // but we should do something better.
            OutlineStyle::Auto => BorderStyle::Solid,
            OutlineStyle::BorderStyle(s) => s,
        };
        let side = self.build_border_side(style, outline.outline_color);
        let details = wr::BorderDetails::Normal(wr::NormalBorder {
            top: side,
            right: side,
            bottom: side,
            left: side,
            radius: offset_radii(self.border_radius, offset),
            do_aa: true,
        });
        builder
            .wr()
            .push_border(&common, outline_rect, widths, details)
    }
}

fn rgba(rgba: cssparser::RGBA) -> wr::ColorF {
    wr::ColorF::new(
        rgba.red_f32(),
        rgba.green_f32(),
        rgba.blue_f32(),
        rgba.alpha_f32(),
    )
}

fn glyphs(
    glyph_runs: &[Arc<GlyphStore>],
    mut origin: PhysicalPoint<Length>,
) -> Vec<wr::GlyphInstance> {
    use gfx_traits::ByteIndex;
    use range::Range;

    let mut glyphs = vec![];
    for run in glyph_runs {
        for glyph in run.iter_glyphs_for_byte_range(&Range::new(ByteIndex(0), run.len())) {
            if !run.is_whitespace() {
                let glyph_offset = glyph.offset().unwrap_or(Point2D::zero());
                let point = units::LayoutPoint::new(
                    origin.x.px() + glyph_offset.x.to_f32_px(),
                    origin.y.px() + glyph_offset.y.to_f32_px(),
                );
                let glyph = wr::GlyphInstance {
                    index: glyph.id(),
                    point,
                };
                glyphs.push(glyph);
            }
            origin.x += Length::from(glyph.advance());
        }
    }
    glyphs
}

fn cursor(kind: CursorKind, auto_cursor: Cursor) -> Cursor {
    match kind {
        CursorKind::Auto => auto_cursor,
        CursorKind::None => Cursor::None,
        CursorKind::Default => Cursor::Default,
        CursorKind::Pointer => Cursor::Pointer,
        CursorKind::ContextMenu => Cursor::ContextMenu,
        CursorKind::Help => Cursor::Help,
        CursorKind::Progress => Cursor::Progress,
        CursorKind::Wait => Cursor::Wait,
        CursorKind::Cell => Cursor::Cell,
        CursorKind::Crosshair => Cursor::Crosshair,
        CursorKind::Text => Cursor::Text,
        CursorKind::VerticalText => Cursor::VerticalText,
        CursorKind::Alias => Cursor::Alias,
        CursorKind::Copy => Cursor::Copy,
        CursorKind::Move => Cursor::Move,
        CursorKind::NoDrop => Cursor::NoDrop,
        CursorKind::NotAllowed => Cursor::NotAllowed,
        CursorKind::Grab => Cursor::Grab,
        CursorKind::Grabbing => Cursor::Grabbing,
        CursorKind::EResize => Cursor::EResize,
        CursorKind::NResize => Cursor::NResize,
        CursorKind::NeResize => Cursor::NeResize,
        CursorKind::NwResize => Cursor::NwResize,
        CursorKind::SResize => Cursor::SResize,
        CursorKind::SeResize => Cursor::SeResize,
        CursorKind::SwResize => Cursor::SwResize,
        CursorKind::WResize => Cursor::WResize,
        CursorKind::EwResize => Cursor::EwResize,
        CursorKind::NsResize => Cursor::NsResize,
        CursorKind::NeswResize => Cursor::NeswResize,
        CursorKind::NwseResize => Cursor::NwseResize,
        CursorKind::ColResize => Cursor::ColResize,
        CursorKind::RowResize => Cursor::RowResize,
        CursorKind::AllScroll => Cursor::AllScroll,
        CursorKind::ZoomIn => Cursor::ZoomIn,
        CursorKind::ZoomOut => Cursor::ZoomOut,
    }
}

fn image_rendering(ir: style::computed_values::image_rendering::T) -> wr::ImageRendering {
    use style::computed_values::image_rendering::T as ImageRendering;
    match ir {
        ImageRendering::Auto => wr::ImageRendering::Auto,
        ImageRendering::CrispEdges => wr::ImageRendering::CrispEdges,
        ImageRendering::Pixelated => wr::ImageRendering::Pixelated,
    }
}

/// Radii for the padding edge or content edge
fn inner_radii(mut radii: wr::BorderRadius, insets: units::LayoutSideOffsets) -> wr::BorderRadius {
    assert!(insets.left >= 0.0, "left inset must not be negative");
    radii.top_left.width -= insets.left;
    radii.bottom_left.width -= insets.left;

    assert!(insets.right >= 0.0, "left inset must not be negative");
    radii.top_right.width -= insets.right;
    radii.bottom_right.width -= insets.right;

    assert!(insets.top >= 0.0, "top inset must not be negative");
    radii.top_left.height -= insets.top;
    radii.top_right.height -= insets.top;

    assert!(insets.bottom >= 0.0, "bottom inset must not be negative");
    radii.bottom_left.height -= insets.bottom;
    radii.bottom_right.height -= insets.bottom;
    radii
}

fn offset_radii(mut radii: wr::BorderRadius, offset: f32) -> wr::BorderRadius {
    if offset == 0.0 {
        return radii;
    }
    if offset < 0.0 {
        return inner_radii(radii, units::LayoutSideOffsets::new_all_same(-offset));
    }
    let expand = |radius: &mut f32| {
        // Expand the radius by the specified amount, but keeping sharp corners.
        // TODO: this behavior is not continuous, it's being discussed in the CSSWG:
        // https://github.com/w3c/csswg-drafts/issues/7103
        if *radius > 0.0 {
            *radius += offset;
        }
    };
    expand(&mut radii.top_left.width);
    expand(&mut radii.top_left.height);
    expand(&mut radii.top_right.width);
    expand(&mut radii.top_right.height);
    expand(&mut radii.bottom_right.width);
    expand(&mut radii.bottom_right.height);
    expand(&mut radii.bottom_left.width);
    expand(&mut radii.bottom_left.height);
    radii
}

fn clip_for_radii(
    radii: wr::BorderRadius,
    rect: units::LayoutRect,
    builder: &mut DisplayListBuilder,
) -> Option<wr::ClipId> {
    if radii.is_zero() {
        None
    } else {
        let parent_space_and_clip = wr::SpaceAndClipInfo {
            spatial_id: builder.current_scroll_node_id.spatial_id,
            clip_id: builder.current_clip_id,
        };
        Some(builder.wr().define_clip_rounded_rect(
            &parent_space_and_clip,
            wr::ComplexClipRegion {
                rect,
                radii,
                mode: wr::ClipMode::Clip,
            },
        ))
    }
}
