/**
 * config/api.ts 的单元测试
 *
 * 覆盖两块此前零测试、但故障后果不对称的逻辑：
 *
 * 1. encodePathPreservingSlashes —— 所有涉及文件路径的 API URL 都过它。本项目
 *    是 Windows 原生应用，路径里必然出现中文、空格、反斜杠、盘符冒号，编码规则
 *    错一处就是 404 或路径穿越，而这类 bug 在界面上表现为「某些文件打不开」，
 *    极难定位到编码函数。所以这里把编码结果写成字面值断言，不用 encodeURIComponent
 *    反过来算期望值——那样等于把实现抄一遍，实现错了测试跟着错。
 *
 * 2. getCurrentUserId / setCurrentUserId / clearCurrentUserId 里的三个 catch 块。
 *    它们防的是隐私模式、配额已满、localStorage 被策略禁用。这类分支平时永不
 *    执行，出问题时却是整页白屏，属于「写了但从没被验证过」的防御代码。
 *
 * 关于第 2 点的踩坑记录（重要，别改回去）：最初用
 * vi.spyOn(window.localStorage, "getItem").mockImplementation(() => { throw ... })
 * 来触发 catch，4 条测试全部通过——但那是恒绿的。反向探针（临时拆掉 api.ts 里的
 * try/catch）跑出来仍然全绿，才发现 spy 压根没接上：jsdom 的 Storage 是 Proxy
 * 实现，vi.spyOn 走 defineProperty 会被 proxy 的 trap 当成「存一个键名为 getItem
 * 的 storage 项」，原方法毫发无伤。于是异常从未抛出，那几条断言实际只依赖
 * 「storage 本来是空的」，与 catch 分支无关；连 expect(getItem).not.toHaveBeenCalled()
 * 也恒真，因为 spy 永远不会被调用。
 *
 * 正确做法是用下面的 FakeStorage 整体替换全局对象（vi.stubGlobal），可显式开关
 * 抛错并记录调用次数。任何改动后都应重跑那个反向探针，确认测试真的会红。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearCurrentUserId,
  encodePathPreservingSlashes,
  getCurrentUserId,
  setCurrentUserId,
} from "../api";
import { getAuthMode } from "../auth";

// api.ts 只从 ./auth 取 getAuthMode 一个符号（注意该 import 语句写在
// api.ts 文件末尾第 582 行，靠 ESM hoisting 才生效，风格异常但行为正确）。
vi.mock("../auth", () => ({
  getAuthMode: vi.fn(() => "local" as "local" | "none"),
}));

/**
 * 可控的 localStorage 替身：用普通对象实现，绕开 jsdom Storage 的 Proxy，
 * 因此 mock 与调用计数都真实生效。failOn 决定哪些操作抛 DOMException。
 */
class FakeStorage {
  private readonly map = new Map<string, string>();
  readonly calls = { getItem: 0, setItem: 0, removeItem: 0 };
  failOn = new Set<"getItem" | "setItem" | "removeItem">();

  private guard(op: "getItem" | "setItem" | "removeItem"): void {
    this.calls[op] += 1;
    if (this.failOn.has(op)) {
      throw new DOMException(`${op} blocked`, "SecurityError");
    }
  }

  getItem(key: string): string | null {
    this.guard("getItem");
    return this.map.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.guard("setItem");
    this.map.set(key, String(value));
  }

  removeItem(key: string): void {
    this.guard("removeItem");
    this.map.delete(key);
  }

  clear(): void {
    this.map.clear();
  }

  key(index: number): string | null {
    return [...this.map.keys()][index] ?? null;
  }

  get length(): number {
    return this.map.size;
  }

  /** 绕过 failOn 与计数，直接布置前置状态 */
  seed(key: string, value: string): void {
    this.map.set(key, value);
  }

  /** 绕过 failOn 直接读，用于断言落盘结果 */
  peek(key: string): string | null {
    return this.map.get(key) ?? null;
  }
}

describe("encodePathPreservingSlashes", () => {
  it("逐段编码，但 / 作为分隔符保留", () => {
    expect(encodePathPreservingSlashes("docs/my file/a.md")).toBe(
      "docs/my%20file/a.md",
    );
  });

  it("中文路径按 UTF-8 编码（本项目最高频场景）", () => {
    expect(encodePathPreservingSlashes("项目/笔记.md")).toBe(
      "%E9%A1%B9%E7%9B%AE/%E7%AC%94%E8%AE%B0.md",
    );
  });

  it("URL 保留字被编码，不会被后端误读为查询串或片段", () => {
    expect(encodePathPreservingSlashes("a/b?c=1&d#frag")).toBe(
      "a/b%3Fc%3D1%26d%23frag",
    );
    expect(encodePathPreservingSlashes("100%/done")).toBe("100%25/done");
    expect(encodePathPreservingSlashes("a+b/c")).toBe("a%2Bb/c");
  });

  // 行为记录，非缺陷判定：Windows 风格路径不会被拆段，整条被当作一个 segment，
  // 反斜杠编码为 %5C、盘符冒号编码为 %3A。这要求所有调用点在传入前已把路径
  // 规范化成正斜杠形式。若某天出现「Windows 绝对路径请求 404」，先查这里。
  it("反斜杠不是分隔符：Windows 路径整体编码", () => {
    expect(encodePathPreservingSlashes("C:\\Users\\ke\\a.md")).toBe(
      "C%3A%5CUsers%5Cke%5Ca.md",
    );
  });

  it("保留空段与首尾斜杠结构，不做路径归一化", () => {
    expect(encodePathPreservingSlashes("")).toBe("");
    expect(encodePathPreservingSlashes("/")).toBe("/");
    expect(encodePathPreservingSlashes("a//b")).toBe("a//b");
    expect(encodePathPreservingSlashes("/a/b/")).toBe("/a/b/");
  });

  it("不幂等：重复调用会双重编码（调用点必须只编码一次）", () => {
    const once = encodePathPreservingSlashes("my file.md");
    expect(once).toBe("my%20file.md");
    expect(encodePathPreservingSlashes(once)).toBe("my%2520file.md");
  });

  it("不编码 RFC3986 未保留字符，避免无谓转义", () => {
    expect(encodePathPreservingSlashes("a-b_c.d~e/f")).toBe("a-b_c.d~e/f");
  });
});

describe("当前用户 ID 的存取", () => {
  let storage: FakeStorage;

  beforeEach(() => {
    vi.mocked(getAuthMode).mockReturnValue("local");
    storage = new FakeStorage();
    vi.stubGlobal("localStorage", storage);
    delete window.__AIASYS_CURRENT_USER_ID__;
  });

  afterEach(() => {
    delete window.__AIASYS_CURRENT_USER_ID__;
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // 这条同时充当 stub 生效性的自检：若 vi.stubGlobal 哪天对裸 localStorage
  // 标识符失效（打包方式或 jsdom 版本变化），calls 计数会停在 0，
  // 下面所有依赖 failOn 的测试就会集体失去意义，这里先把它拦住。
  it("替身已真正接管全局 localStorage（其余用例的前提）", () => {
    getCurrentUserId();
    expect(storage.calls.getItem).toBeGreaterThan(0);
  });

  it("authMode=none 时返回固定的匿名 ID，且完全不读 localStorage", () => {
    vi.mocked(getAuthMode).mockReturnValue("none");
    storage.seed("user_id", "should_be_ignored");

    expect(getCurrentUserId()).toBe("test_anonymous_dev");
    expect(storage.calls.getItem).toBe(0);
  });

  it("local 模式无任何存量值时回落到 local_default", () => {
    expect(getCurrentUserId()).toBe("local_default");
  });

  it("set 后 get 能取回，且同时落盘到 localStorage", () => {
    setCurrentUserId("u-123");
    expect(getCurrentUserId()).toBe("u-123");
    expect(storage.peek("user_id")).toBe("u-123");
  });

  it("window 上的值优先于 localStorage", () => {
    storage.seed("user_id", "from-storage");
    setCurrentUserId("from-window");
    expect(getCurrentUserId()).toBe("from-window");
  });

  it("仅有 localStorage 时（如刷新后首次读）能恢复", () => {
    storage.seed("user_id", "persisted");
    expect(getCurrentUserId()).toBe("persisted");
  });

  it("clear 后清空两处存储并回落默认值", () => {
    setCurrentUserId("u-123");
    clearCurrentUserId();
    expect(storage.peek("user_id")).toBeNull();
    expect(getCurrentUserId()).toBe("local_default");
  });

  // 这条最初我写的期望是「空串被忽略、原值保留」，实测发现行为相反且更糟：
  // setCurrentUserId 无条件 setItem，空串会覆盖掉已持久化的有效 ID，随后
  // getCurrentUserId 的 || 链跳过这个 falsy 值，静默回落 local_default——
  // 用户身份就这么丢了，界面上没有任何报错。
  //
  // 当前不可达，所以不改实现：两个调用点分别是 contexts/AuthContext.tsx:172
  // （传硬编码常量 "test_anonymous_dev"）和 :220（在 if (data?.user?.id) 的
  // truthy 保护内），都传不进空串。留这条断言是因为一旦有人写成
  // setCurrentUserId(user?.id ?? "") 或 setCurrentUserId(resp.id || "")，
  // 这是唯一会出声的地方。若未来决定加入参数校验，此测试应当变红并被改写。
  it("写入空串会擦掉已持久化的有效 ID（破坏性，调用点须自行拦截 falsy）", () => {
    storage.seed("user_id", "persisted");
    setCurrentUserId("");
    expect(storage.peek("user_id")).toBe("");
    expect(getCurrentUserId()).toBe("local_default");
  });

  describe("localStorage 不可用时（隐私模式 / 配额满 / 策略禁用）", () => {
    it("读取抛错时不崩溃，回落到默认值", () => {
      storage.seed("user_id", "persisted");
      storage.failOn.add("getItem");
      expect(() => getCurrentUserId()).not.toThrow();
      expect(getCurrentUserId()).toBe("local_default");
    });

    it("读取抛错但内存中已有值时，仍能返回该值", () => {
      setCurrentUserId("u-mem");
      storage.failOn.add("getItem");
      expect(getCurrentUserId()).toBe("u-mem");
    });

    it("写入抛错时不崩溃，且内存态仍然生效", () => {
      storage.failOn.add("setItem");
      expect(() => setCurrentUserId("u-quota")).not.toThrow();
      expect(storage.peek("user_id")).toBeNull();
      expect(getCurrentUserId()).toBe("u-quota");
    });

    it("清除抛错时不崩溃，且内存态已被清掉", () => {
      setCurrentUserId("u-1");
      storage.failOn.add("removeItem");
      expect(() => clearCurrentUserId()).not.toThrow();
      expect(window.__AIASYS_CURRENT_USER_ID__).toBeUndefined();
      // localStorage 里的残值确实还在（removeItem 失败），这是既有行为：
      // 下次读取会把它当作有效身份恢复出来。清除失败后的一致性问题留待
      // 产品决定，此处只固化现状。
      expect(storage.peek("user_id")).toBe("u-1");
    });
  });
});
