import { desktopInvokeQuiet } from "./desktop.ts";

export const BROWSER_PROFILE_WIRE_MESSAGE = "This Mac app has not confirmed support for the saved OpenAI browser profile. Update the Mac app before preparing or approving this profile. Existing task results, cancellation and reconciliation use their separate controls.";

/** Parser support only; provider availability and the saved profile digest remain separate gates. */
export function browserProfileWireSupported(value: unknown, version = "browser_cuai_text_v1", readOnly = false, qualification = false): boolean {
  return !!value && typeof value === "object" && !Array.isArray(value)
    && ["browser_cuai_text_v1", "browser_cuai_text_v2"].includes(version)
    && typeof readOnly === "boolean" && typeof qualification === "boolean"
    && Object.keys(value).every(key => ["browser_cuai_text_v1", "browser_cuai_text_v2", "pagebuilder_reference_v1", "browser_cuai_read_only_v1", "browser_qualification_v1", "product_build_v1"].includes(key))
    && Object.values(value).every(marker => typeof marker === "boolean")
    && (value as Record<string, unknown>)[version] === true
    && (!readOnly || (value as Record<string, unknown>).browser_cuai_read_only_v1 === true)
    && (!qualification || (value as Record<string, unknown>).browser_qualification_v1 === true);
}

/** Older app builds lack this command. That is unavailable, without a noisy native error or fallback. */
export async function browserProfileWireAvailable(version = "browser_cuai_text_v1", readOnly = false, qualification = false): Promise<boolean> {
  return browserProfileWireSupported(await desktopInvokeQuiet("device_work_order_capabilities"), version, readOnly, qualification);
}
