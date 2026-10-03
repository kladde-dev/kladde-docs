//! The heap that opening a drawing allocates, and the median time of 21 opens
//! and parses: `memory <drawing.svg> <scratch.kladde>`, one line of
//! `memory.txt`. `memory --empty` prints an empty drawing's live bytes and
//! the model's sizes, as in `empty.txt`.
//!
//! Built as the binary of a crate that depends on kladde and kladde-svg by
//! path, with features `packed`, `small-ids`, `small-attrs` and `small` that
//! turn on kladde-svg's feature of the same name.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering::Relaxed};

struct Counting;
static LIVE: AtomicUsize = AtomicUsize::new(0);
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 { LIVE.fetch_add(l.size(), Relaxed); unsafe { System.alloc(l) } }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) { LIVE.fetch_sub(l.size(), Relaxed); unsafe { System.dealloc(p, l) } }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        LIVE.fetch_add(n, Relaxed); LIVE.fetch_sub(l.size(), Relaxed); unsafe { System.realloc(p, l, n) }
    }
}
#[global_allocator]
static A: Counting = Counting;

fn main() {
    let src = std::env::args().nth(1).unwrap();
    if src == "--empty" {
        let doc = kladde_svg::parse(r#"<svg xmlns="http://www.w3.org/2000/svg"/>"#).unwrap();
        let k = kladde::Kladde::create_in(
            Box::new(kladde::MemoryStorage::new()),
            doc,
            kladde::Options::default(),
        )
        .unwrap();
        let s = k.stats();
        println!(
            "empty: alloc_bytes={} allocations={} size_of Element={} Node={} PathSegment={} Attr={}",
            s.allocation_bytes,
            s.allocations,
            std::mem::size_of::<kladde_svg::model::Element>(),
            std::mem::size_of::<kladde_svg::model::Node>(),
            std::mem::size_of::<kladde_svg::model::PathSegment>(),
            std::mem::size_of::<kladde_svg::model::Attr>()
        );
        use kladde::Persistable;
        use kladde_svg::model::*;
        println!(
            "size_of Transforms={} Attrs={} Nodes={} PathData={} ElementKind={}; slotted Attr={:?} Paint={:?} Node={:?} Element={:?} PathSegment={:?}",
            std::mem::size_of::<Transforms>(),
            std::mem::size_of::<Attrs>(),
            std::mem::size_of::<Nodes>(),
            std::mem::size_of::<PathData>(),
            std::mem::size_of::<ElementKind>(),
            <Attr as Persistable>::SLOTTED_SIZE,
            <Paint as Persistable>::SLOTTED_SIZE,
            <Node as Persistable>::SLOTTED_SIZE,
            <Element as Persistable>::SLOTTED_SIZE,
            <PathSegment as Persistable>::SLOTTED_SIZE,
        );
        return;
    }
    let path = std::env::args().nth(2).unwrap();
    let text = std::fs::read_to_string(&src).unwrap();
    let base = LIVE.load(Relaxed);
    let doc = kladde_svg::parse(&text).unwrap();
    let model = LIVE.load(Relaxed) - base;
    let k = kladde::Kladde::create(&path, doc).unwrap();
    k.close().unwrap();
    let base = LIVE.load(Relaxed);
    let k = kladde::Kladde::<kladde_svg::Document>::open(&path).unwrap();
    let opened = LIVE.load(Relaxed) - base;
    drop(k);
    let mut open_us = Vec::new();
    let mut parse_us = Vec::new();
    for _ in 0..21 {
        let t = std::time::Instant::now();
        let k = kladde::Kladde::<kladde_svg::Document>::open(&path).unwrap();
        open_us.push(t.elapsed().as_micros());
        drop(k);
        let t = std::time::Instant::now();
        let doc = kladde_svg::parse(&text).unwrap();
        parse_us.push(t.elapsed().as_micros());
        drop(doc);
    }
    open_us.sort();
    parse_us.sort();
    println!("model_parsed={model} opened={opened} open_us={} parse_us={}", open_us[10], parse_us[10]);
    std::fs::remove_file(&path).ok();
}
