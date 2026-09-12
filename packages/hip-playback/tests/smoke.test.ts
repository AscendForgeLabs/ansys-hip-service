/** 脚手架冒烟:构建/测试管线就位。 */
import { expect, it } from "vitest";

import { VERSION } from "../src/index";

it("导出版本号", () => {
  expect(VERSION).toMatch(/^\d+\.\d+\.\d+$/);
});
