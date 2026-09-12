/** three.js 场景 — cube.template.html 渲染核心的类化移植(three 钉 0.128 同版)。

 * 六步渲染配方(playback-handbook §2)全部在此:
 * 共享 position/color BufferAttribute(一次 needsUpdate)、静态面法线、
 * Phong 双面(shininess=0,凹坑内壁才有光)、viridis |u| 云图、
 * 帧间线性插值、变形放大 pos = 基准 + scale×u。
 * cube 型(lattice)额外带参考轮廓与压头;通用(emap)网格无此二者。
 */

import * as THREE from "three";

import type { MeshData } from "./mesh/types";
import { depthAt, segOf } from "./playback-model";
import { VIRIDIS_STOPS } from "./colormap";

/** 压头几何口径:cube_dent 压头 4mm / 立方边 20mm;顶段高度 4mm。 */
const PUNCH_WIDTH_RATIO = 0.2;
const PUNCH_TOP = 4;

export interface FrameHud {
  uMax: number;          // 当前时刻 max|u|(mm)
  depth: number;         // 压深(顶面压头类载荷口径;通用网格同样按 |min uy| 报)
}

export class PlaybackScene {
  private readonly renderer: THREE.WebGLRenderer;
  private readonly scene = new THREE.Scene();
  private readonly camera: THREE.PerspectiveCamera;
  private readonly group = new THREE.Group();
  private readonly canvas: HTMLCanvasElement;

  private posAttr: THREE.BufferAttribute | null = null;
  private colAttr: THREE.BufferAttribute | null = null;
  private basePos: Float32Array = new Float32Array(0);
  private framesU: Float32Array[] = [];
  private nv = 0;
  private nseg = 0;
  private colorMax = 1;
  private stagesDepth: number[] = [0];
  private faceMeshes: THREE.Mesh[] = [];
  private wireMeshes: THREE.LineSegments[] = [];
  private ghost: THREE.LineSegments | null = null;
  private punch: THREE.Mesh | null = null;
  private punchWidth = 1;
  private bboxMin = new THREE.Vector3();
  private bboxMax = new THREE.Vector3();
  private maxDim = 20;                     // 包围盒最大边长(相机尺度基准)
  private mode: "lattice" | "emap" = "lattice";

  // 轨道相机(拖转 + 滚轮;参数与参考实现同款,尺度按包围盒自适应)
  private yaw = 0.62;
  private pitch = 0.95;
  private dist = 95;
  private distMin = 45;
  private distMax = 400;
  private center = new THREE.Vector3();
  private drag: [number, number] | null = null;
  private readonly onDragMove = (e: MouseEvent): void => {
    if (!this.drag) return;
    this.yaw += (e.clientX - this.drag[0]) * 0.008;
    this.pitch = Math.max(-0.05, Math.min(1.35, this.pitch + (e.clientY - this.drag[1]) * 0.006));
    this.drag = [e.clientX, e.clientY];
  };
  private readonly onDragEnd = (): void => { this.drag = null; };
  private readonly onWheel = (e: WheelEvent): void => {
    e.preventDefault();
    this.dist = Math.max(
      this.distMin, Math.min(this.distMax, this.dist * (e.deltaY > 0 ? 1.08 : 0.93)));
  };

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.scene.background = new THREE.Color(0x0e1116);
    this.camera = new THREE.PerspectiveCamera(42, 1, 1, 4000);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x30363f, 0.95));
    const main = new THREE.DirectionalLight(0xffffff, 0.55);
    main.position.set(0.6, 1, 0.8);
    this.scene.add(main);
    const fill = new THREE.DirectionalLight(0xaebfd4, 0.35);   // 低角度补光照凹坑内壁
    fill.position.set(-0.7, 0.25, -0.6);
    this.scene.add(fill);
    this.scene.add(this.group);

    canvas.addEventListener("mousedown", (e) => { this.drag = [e.clientX, e.clientY]; });
    addEventListener("mousemove", this.onDragMove);
    addEventListener("mouseup", this.onDragEnd);
    canvas.addEventListener("wheel", this.onWheel, { passive: false });
  }

  /** 装配/替换网格数据(共享顶点缓冲、六面外壳 + 网格线 + lattice 附加物)。 */
  setMesh(data: MeshData): void {
    this.disposeMeshes();
    this.nv = data.vertBase.length / 3;
    this.nseg = data.meta.nseg;
    this.colorMax = data.meta.colorMax || 1;
    this.stagesDepth = data.stages.depth;
    this.basePos = Float32Array.from(data.vertBase);
    this.framesU = data.framesU.map((f) => Float32Array.from(f));
    this.mode = data.mode;

    this.posAttr = new THREE.BufferAttribute(new Float32Array(this.nv * 3), 3);
    this.colAttr = new THREE.BufferAttribute(new Float32Array(this.nv * 3), 3);
    const nrmAttr = new THREE.BufferAttribute(Float32Array.from(data.vertNorm), 3);

    // 包围盒(相机中心/距离自适应;参考实现的立方中心是其特例)
    this.bboxMin.set(Infinity, Infinity, Infinity);
    this.bboxMax.set(-Infinity, -Infinity, -Infinity);
    for (let i = 0; i < this.basePos.length; i += 3) {
      for (let d = 0; d < 3; d++) {
        const v = this.basePos[i + d]!;
        if (v < this.bboxMin.getComponent(d)) this.bboxMin.setComponent(d, v);
        if (v > this.bboxMax.getComponent(d)) this.bboxMax.setComponent(d, v);
      }
    }
    const size = new THREE.Vector3().subVectors(this.bboxMax, this.bboxMin);
    this.maxDim = Math.max(size.x, size.y, size.z) || 1;
    this.center.addVectors(this.bboxMin, this.bboxMax).multiplyScalar(0.5);
    this.distMin = this.maxDim * 2.25;
    this.distMax = this.maxDim * 20;

    const phong = new THREE.MeshPhongMaterial({
      vertexColors: true, side: THREE.DoubleSide, shininess: 0, specular: 0x000000,
    });
    const wireMat = new THREE.LineBasicMaterial({
      color: 0x9db0c3, transparent: true, opacity: 0.18,
    });
    for (const face of data.faces) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", this.posAttr);
      g.setAttribute("color", this.colAttr);
      g.setAttribute("normal", nrmAttr);
      g.setIndex(face.tris);
      this.faceMeshes.push(new THREE.Mesh(g, phong));
      const wg = new THREE.BufferGeometry();
      wg.setAttribute("position", this.posAttr);
      wg.setIndex(face.wire);
      this.wireMeshes.push(new THREE.LineSegments(wg, wireMat));
      this.group.add(this.faceMeshes.at(-1)!, this.wireMeshes.at(-1)!);
    }

    if (this.mode === "lattice") {
      const side = this.maxDim;
      const box = new THREE.BoxGeometry(size.x || side, size.y || side, size.z || side);
      this.ghost = new THREE.LineSegments(
        new THREE.EdgesGeometry(box),
        new THREE.LineDashedMaterial({
          color: 0x5d6a78, dashSize: 1.6, gapSize: 1.2, transparent: true, opacity: 0.6,
        }));
      this.ghost.computeLineDistances();
      this.ghost.position.copy(this.center);
      this.group.add(this.ghost);

      this.punchWidth = side * PUNCH_WIDTH_RATIO;
      this.punch = new THREE.Mesh(
        new THREE.BoxGeometry(this.punchWidth, 1, this.punchWidth),
        new THREE.MeshLambertMaterial({ color: 0x8fa3b8, transparent: true, opacity: 0.22 }));
      this.group.add(this.punch);
    }
    this.resetView();
  }

  /** 逐顶点更新当前时刻:插值位移 → pos = 基准 + scale×u;着色 |u|/max → viridis。 */
  update(t: number, scale: number): FrameHud {
    const seg = segOf(t, this.nseg);
    const pos = this.posAttr!.array as Float32Array;
    const col = this.colAttr!.array as Float32Array;
    let uMax = 0;
    for (let i = 0; i < this.nv; i++) {
      const o = i * 3;
      let ux = 0, uy = 0, uz = 0;
      if (seg) {
        const a = this.framesU[seg[0]]!;
        const b = this.framesU[seg[0] + 1]!;
        const f = seg[1];
        ux = a[o]! + (b[o]! - a[o]!) * f;
        uy = a[o + 1]! + (b[o + 1]! - a[o + 1]!) * f;
        uz = a[o + 2]! + (b[o + 2]! - a[o + 2]!) * f;
      }
      const u = Math.sqrt(ux * ux + uy * uy + uz * uz);
      if (u > uMax) uMax = u;
      pos[o] = this.basePos[o]! + scale * ux;
      pos[o + 1] = this.basePos[o + 1]! + scale * uy;
      pos[o + 2] = this.basePos[o + 2]! + scale * uz;
      // viridis 写入(内联避免逐顶点分配;与 colormap.ts 同参数)
      const v = Math.max(0, Math.min(1, u / this.colorMax)) * (VIRIDIS_STOPS.length - 1);
      const si = Math.min(Math.floor(v), VIRIDIS_STOPS.length - 2);
      const sf = v - si;
      const a = VIRIDIS_STOPS[si]!;
      const b = VIRIDIS_STOPS[si + 1]!;
      col[o] = (a[0]! + (b[0]! - a[0]!) * sf) / 255;
      col[o + 1] = (a[1]! + (b[1]! - a[1]!) * sf) / 255;
      col[o + 2] = (a[2]! + (b[2]! - a[2]!) * sf) / 255;
    }
    this.posAttr!.needsUpdate = true;
    this.colAttr!.needsUpdate = true;

    const depth = depthAt(t, this.nseg, this.stagesDepth);
    if (this.punch) {                       // 压头底面跟随压深,顶段始终露在立方上方
      const ph = scale * depth + PUNCH_TOP;
      this.punch.scale.y = ph;
      this.punch.position.set(
        this.center.x, this.bboxMax.y - scale * depth + ph / 2, this.center.z);
    }
    return { uMax, depth };
  }

  render(): void {
    const cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    this.camera.position.set(
      this.center.x + this.dist * cp * Math.cos(this.yaw),
      this.center.y + this.dist * sp,
      this.center.z + this.dist * cp * Math.sin(this.yaw));
    this.camera.lookAt(this.center);
    this.renderer.render(this.scene, this.camera);
  }

  resetView(): void {
    this.yaw = 0.62;
    this.pitch = 0.95;
    // 参考实现口径:side 20 → dist 95(4.75×),钳在 [2.25×, 20×]
    this.dist = Math.max(this.distMin, Math.min(this.distMax, this.maxDim * 4.75));
  }

  resize(width: number, height: number): void {
    if (width <= 0 || height <= 0) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  setLayerVisible(layer: "shell" | "wire" | "ghost" | "punch", visible: boolean): void {
    if (layer === "shell") { for (const m of this.faceMeshes) m.visible = visible; }
    else if (layer === "wire") { for (const m of this.wireMeshes) m.visible = visible; }
    else if (layer === "ghost" && this.ghost) this.ghost.visible = visible;
    else if (layer === "punch" && this.punch) this.punch.visible = visible;
  }

  /** 顶点级对账 dump(与参考实现 #debug 同构;headless 验证/排查渲染问题用)。 */
  debugInfo(t: number, scale: number): Record<string, unknown> {
    this.update(t, scale);
    const last = this.framesU[this.framesU.length - 1]!;
    let topVert = 0, uMag = -1;
    for (let i = 0; i < this.nv; i++) {
      const o = i * 3;
      const u = Math.sqrt(last[o]! ** 2 + last[o + 1]! ** 2 + last[o + 2]! ** 2);
      if (u > uMag) { uMag = u; topVert = i; }
    }
    const o = topVert * 3;
    const pos = this.posAttr!.array as Float32Array;
    this.render();
    this.camera.updateMatrixWorld(true);
    this.camera.matrixWorldInverse.copy(this.camera.matrixWorld).invert();
    const v3 = new THREE.Vector3(pos[o]!, pos[o + 1]!, pos[o + 2]!).project(this.camera);
    const canvasBox = this.canvas.getBoundingClientRect();
    const screen = [
      Math.round((v3.x + 1) / 2 * canvasBox.width),
      Math.round((1 - v3.y) / 2 * canvasBox.height),
    ];
    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(v3.x, v3.y), this.camera);
    const hits = ray.intersectObjects(this.faceMeshes, false).slice(0, 3).map((h) => {
      const face = h.face!;
      const p = face.a * 3;
      const col = this.colAttr!.array as Float32Array;
      return {
        tri: [face.a, face.b, face.c],
        col: [col[p]!, col[p + 1]!, col[p + 2]!].map((v) => +v.toFixed(3)),
        dist: +h.distance.toFixed(1),
      };
    });
    return {
      mode: this.mode, nv: this.nv, nseg: this.nseg, uxmax: this.colorMax,
      seg: segOf(t, this.nseg), topVert, uMag: +uMag.toFixed(5),
      base: [this.basePos[o]!, this.basePos[o + 1]!, this.basePos[o + 2]!],
      lastFrame: [last[o]!, last[o + 1]!, last[o + 2]!],
      pos: [pos[o]!, pos[o + 1]!, pos[o + 2]!].map((v) => +v.toFixed(2)),
      screen, hits,
    };
  }

  dispose(): void {
    removeEventListener("mousemove", this.onDragMove);
    removeEventListener("mouseup", this.onDragEnd);
    this.disposeMeshes();
    this.renderer.dispose();
  }

  private disposeMeshes(): void {
    for (const m of [...this.faceMeshes, ...this.wireMeshes]) {
      this.group.remove(m);
      m.geometry.dispose();
      (m.material as THREE.Material).dispose();
    }
    if (this.ghost) { this.group.remove(this.ghost); this.ghost.geometry.dispose(); (this.ghost.material as THREE.Material).dispose(); }
    if (this.punch) { this.group.remove(this.punch); this.punch.geometry.dispose(); (this.punch.material as THREE.Material).dispose(); }
    this.faceMeshes = [];
    this.wireMeshes = [];
    this.ghost = null;
    this.punch = null;
  }
}
