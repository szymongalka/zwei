/** Fork identity. Runtime commands, storage keys and upstream provenance stay stable. */
export const PRODUCT_NAME = "Zwei";
export const PRODUCT_ICON = "/brand/zwei.svg";
export const PRODUCT_REPOSITORY = "https://github.com/szymongalka/zwei";

type CommonMessages = typeof import("../i18n/locales/en/common.json");

export function withProductBrand(messages: CommonMessages): CommonMessages {
  const app = messages.app;
  const rename = (text: string) => text.replace(/nanobot(?:owi|em|a)?/gi, PRODUCT_NAME);
  // Only product copy is overridden; commands, paths and translation keys retain
  // their upstream meanings. Keep the locale source files independently upstreamable.
  return {
    ...messages,
    app: {
      ...app,
      brand: PRODUCT_NAME,
      loading: { connecting: rename(app.loading.connecting), boot: rename(app.loading.boot) },
      error: { ...app.error, title: rename(app.error.title) },
      auth: { ...app.auth, title: rename(app.auth.title), helpConfig: rename(app.auth.helpConfig) },
      system: {
        ...app.system,
        restart: rename(app.system.restart),
        restartHint: rename(app.system.restartHint),
      },
      documentTitle: { base: PRODUCT_NAME, chat: `{{title}} · ${PRODUCT_NAME}` },
      meta: { description: rename(app.meta.description) },
    },
  };
}
