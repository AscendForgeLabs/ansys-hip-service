// 构建配置:库模式 IIFE 单文件(dist/hip-playback.js)。
// three@0.128 与参考实现(cube.template.html 用的 three.min.js r128)钉同版本,
// 保证渲染逐参数一致(真机验收图像素级对拍的前提)。three 随 bundle 内联,零外链。
import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    lib: {
      entry: "src/index.ts",
      name: "HIPPlayback",
      formats: ["iife"],
      fileName: () => "hip-playback.js",
    },
    // 头部保留 three.js MIT 许可声明与构建信息
    rollupOptions: {
      output: {
        banner: [
          "/*! hip-playback — ansys-hip 帧工件 3D 回放组件(MIT) */",
          "/*! 内联 three.js r128 — Copyright 2010-2021 Three.js Authors — MIT License */",
        ].join("\n"),
      },
    },
  },
  test: {
    environment: "happy-dom",
    include: ["tests/**/*.test.ts"],
  },
});
