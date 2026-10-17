#!/usr/bin/env bash
# 不用 gradle,直接用 build-tools 链:aapt2 → javac → d8 → zipalign → apksigner。
# 依赖:brew install openjdk@17 && brew install --cask android-commandlinetools
#       sdkmanager "platforms;android-33" "build-tools;34.0.0"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_SDK_ROOT:-/opt/homebrew/share/android-commandlinetools}"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-33/android.jar"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export PATH="$JAVA_HOME/bin:$BT:$PATH"
OUT="$HERE/build"
rm -rf "$OUT" && mkdir -p "$OUT/gen" "$OUT/classes" "$OUT/dex"

aapt2 compile --dir "$HERE/res" -o "$OUT/res.zip"
aapt2 link -o "$OUT/base.apk" -I "$JAR" --manifest "$HERE/AndroidManifest.xml" \
    -R "$OUT/res.zip" --java "$OUT/gen" --auto-add-overlay --debug-mode
javac -source 11 -target 11 -nowarn -cp "$JAR" -d "$OUT/classes" \
    $(find "$OUT/gen" "$HERE/src" -name '*.java')
d8 --release --lib "$JAR" --min-api 26 --output "$OUT/dex" $(find "$OUT/classes" -name '*.class')
(cd "$OUT/dex" && zip -q -u ../base.apk classes.dex)
zipalign -f 4 "$OUT/base.apk" "$OUT/aligned.apk"
# 签名 key 固定在源码目录(只生成一次),否则每次重编换 key,adb install -r 会因签名不一致静默失败
KS="$HERE/debug.keystore"
[ -f "$KS" ] || keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias androiddebugkey \
    -dname "CN=Android Debug,O=Android,C=US" -keyalg RSA -keysize 2048 -validity 10000 >/dev/null 2>&1
apksigner sign --ks "$KS" --ks-pass pass:android --key-pass pass:android --ks-key-alias androiddebugkey \
    --out "$HERE/../apk/MockQidian.apk" "$OUT/aligned.apk"
echo "built: $HERE/../apk/MockQidian.apk"
