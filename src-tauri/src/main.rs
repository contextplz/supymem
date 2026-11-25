#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    #[cfg(feature = "local-stack")]
    {
        let args: Vec<_> = std::env::args_os().skip(1).collect();
        if args.first().is_some_and(|arg| arg == "--local-stack-smoke") {
            if args.len() != 3 {
                eprintln!("Choose absolute resource and disposable data folders, then retry your smoke test.");
                std::process::exit(2);
            }
            if let Err(error) = company_os_desktop_lib::local_stack_smoke(args[1].clone().into(), args[2].clone().into()) {
                eprintln!("{error}");
                std::process::exit(1);
            }
            return;
        }
    }
    company_os_desktop_lib::run();
}
