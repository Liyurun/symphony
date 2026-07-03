import type { ProviderStreams } from "../types.ts";
import { lazyApi } from "./lazy.ts";

export const miraApi = (): ProviderStreams => lazyApi(() => import("./mira.ts"));
