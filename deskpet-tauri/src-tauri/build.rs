use std::path::{Path, PathBuf};

fn main() {
    tauri_build::build();
    gen_sample_pack();
}

/// 遍历 `../sample-pack/`，生成 `SAMPLE_FILES: &[(&str, &[u8])]` 供运行期首次启动释放。
/// 没有 idle 帧时用仓库内的程序图标当占位帧（不额外引入二进制素材）。
fn gen_sample_pack() {
    let manifest = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let root = manifest.parent().unwrap().join("sample-pack");
    println!("cargo:rerun-if-changed={}", root.display());

    let mut entries: Vec<(String, String)> = Vec::new();
    walk(&root, &root, &mut entries);
    entries.retain(|(rel, _)| !rel.starts_with('.') && !rel.to_ascii_lowercase().ends_with(".md"));
    entries.sort();

    let icon = manifest.join("icons").join("128x128.png");
    if icon.is_file() && !entries.iter().any(|(rel, _)| rel == "animations/sample_idle/f0000.png")
    {
        entries.push((
            "animations/sample_idle/f0000.png".to_string(),
            abs(&icon),
        ));
    }

    let mut code = String::from("pub static SAMPLE_FILES: &[(&str, &[u8])] = &[\n");
    for (rel, src) in &entries {
        code.push_str(&format!("    ({:?}, include_bytes!({:?})),\n", rel, src));
    }
    code.push_str("];\n");

    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("sample_pack.rs");
    std::fs::write(&out, code).expect("write sample_pack.rs");
}

fn walk(dir: &Path, base: &Path, out: &mut Vec<(String, String)>) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, base, out);
        } else if p.is_file() {
            let rel = p
                .strip_prefix(base)
                .unwrap_or(&p)
                .to_string_lossy()
                .replace('\\', "/");
            out.push((rel, abs(&p)));
        }
    }
}

fn abs(p: &Path) -> String {
    p.canonicalize()
        .unwrap_or_else(|_| p.to_path_buf())
        .to_string_lossy()
        .replace('\\', "/")
}
