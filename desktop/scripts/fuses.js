// Flip Electron fuses on the packaged app so it can't be repurposed:
// no running as plain Node, no --inspect debugging, only load the signed app archive,
// and cookies encrypted at rest. Stops a local process from launching the app in debug mode
// to read the stored sign-in token.
const path = require("path");
const { flipFuses, FuseVersion, FuseV1Options } = require("@electron/fuses");

exports.default = async function afterPack(context) {
  const { electronPlatformName, appOutDir, packager } = context;
  const name = packager.appInfo.productFilename;
  const exe = electronPlatformName === "darwin"
    ? path.join(appOutDir, `${name}.app`)
    : path.join(appOutDir, `${name}.exe`);
  await flipFuses(exe, {
    version: FuseVersion.V1,
    resetAdHocDarwinSignature: electronPlatformName === "darwin",
    [FuseV1Options.RunAsNode]: false,
    [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
    [FuseV1Options.EnableNodeCliInspectArguments]: false,
    [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]: true,
    [FuseV1Options.OnlyLoadAppFromAsar]: true,
    [FuseV1Options.EnableCookieEncryption]: true,
  });
};
