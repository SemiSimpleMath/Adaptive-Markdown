---
doc_id: d-01KRJ4KNN0TEDGCW
source_url: https://arxiv.org/abs/math/0202159
title: An Elementary Proof of Apéry's Theorem
authors: ["Wadim Zudilin (Moscow Lomonosov State University)"]
fetched: 2026-05-13
license: arXiv non-exclusive license to distribute
audience: expert
language: en
---

# An Elementary Proof of Apéry's Theorem

**E-print:** [math.NT/0202159](https://arxiv.org/abs/math/0202159) (17 February 2002)

## Abstract

We present a new "elementary" proof of the irrationality of $\zeta(3)$ based on some recent "hypergeometric" ideas of Yu. Nesterenko, T. Rivoal, and K. Ball, and on Zeilberger's algorithm of creative telescoping.

*Editorial note.* This is an adaptive-markdown edition of the original arXiv preprint [math.NT/0202159](https://arxiv.org/abs/math/0202159), not a verbatim reproduction. The mathematical content is the author's; the surrounding prose has been adapted for the adaptive-markdown reader — section headings have been added or relabelled, an [Aside for a high school reader](#aside-highschool) and a [dependency-graph figure](#dag) have been inserted, the proof of [Lemma 3](#lemma-3) has been written out from the original sketch, and the proof of [Apéry's Theorem](#aperys-proof) has been broken into explicit steps. Lemmas 4–7 remain as statements only; ask the agent to expand any of them. Treat the source paper, not this rendering, as the citable reference.

**Keywords.** Zeta value, hypergeometric series, Apéry's theorem, Zeilberger's algorithm of creative telescoping.

## Introduction

A question of an arithmetic nature of the values of Riemann's zeta function

$$\zeta(s) := \sum_{n=1}^\infty \frac{1}{n^s}$$

at odd integral points $s = 3, 5, 7, \ldots$ looks like a challenge for Number Theory. An expected answer "*each odd zeta value is transcendental*" is still far from being proved. We dispose of a particular information on the *irrationality* of odd zeta values, namely:

- $\zeta(3)$ is irrational (R. Apéry [[Ap]](#bib-Ap), 1978);
- infinitely many of the numbers $\zeta(3), \zeta(5), \zeta(7), \ldots$ are irrational (T. Rivoal [[Ri1]](#bib-Ri1), [[BR]](#bib-BR), 2000);
- each set $\zeta(s+2), \zeta(s+4), \ldots, \zeta(8s-3), \zeta(8s-1)$ with odd $s > 1$ contains at least one irrational number (this author [[Zu1]](#bib-Zu1), [[Zu2]](#bib-Zu2), 2001);
- at least one of the four numbers $\zeta(5), \zeta(7), \zeta(9), \zeta(11)$ is irrational (this author [[Zu3]](#bib-Zu3), [[Zu4]](#bib-Zu4), 2001).

All these results have a *classical* well-poised-hypergeometric origin. The aim of this note is to prove Apéry's famous result by "elementary means."

## Aside (For a high school reader) {#aside-highschool}

If the page above looks like a wall of symbols, here is what it is actually saying.

**What is $\zeta(s)$?** It is just an infinite sum. Plug in a number $s$ and add up the reciprocals of $1, 2, 3, \ldots$ each raised to the power $s$:

$$\zeta(2) = \frac{1}{1^2} + \frac{1}{2^2} + \frac{1}{3^2} + \frac{1}{4^2} + \cdots, \qquad \zeta(3) = \frac{1}{1^3} + \frac{1}{2^3} + \frac{1}{3^3} + \cdots$$

As long as $s > 1$, the sum settles down to a finite value. So $\zeta(3)$ is a specific real number — about $1.2020569\ldots$ — built out of pure whole-number information.

**What the bullets above say, in plain English.**

- **1978 (Apéry).** $\zeta(3)$ is not a fraction. This is the theorem this paper proves.
- **2000 (Rivoal).** Of the infinite list $\zeta(3), \zeta(5), \zeta(7), \ldots$, infinitely many entries are not fractions — but we cannot point to which ones (beyond $\zeta(3)$).
- **2001 (Zudilin).** In any sufficiently long window of odd zeta values, at least one is irrational.
- **2001 (Zudilin again).** Among $\zeta(5), \zeta(7), \zeta(9), \zeta(11)$, at least one is irrational. We just do not know which.

Notice how weak these statements are compared to what we *believe*: every single $\zeta(3), \zeta(5), \zeta(7), \ldots$ should be irrational, and in fact transcendental. We cannot even prove $\zeta(5)$ alone is irrational. Number theory is humbling.

**What this paper does.** Apéry's original 1978 proof was famously mysterious — people did not believe it at first. This note re-proves the same theorem ($\zeta(3)$ is irrational) using more recent ideas, in a way the author calls "elementary." Elementary here means "no advanced analytic machinery" — not "easy." The strategy is the classical trick for proving a number $\alpha$ is irrational: assume $\alpha = p/q$ and construct a sequence of integers that the assumption forces to be both positive and smaller than $1$. Integers cannot do that, so the assumption was wrong.

## Theorem (Apéry's Theorem) {#aperys-theorem}

**Statement.** The number $\zeta(3)$ is irrational.

The idea of the following proof is due to T. Rivoal [[Ri2]](#bib-Ri2), [[Ri3]](#bib-Ri3), who mixed approaches of Yu. Nesterenko [[Ne]](#bib-Ne) and K. Ball, and our contribution here is to make use of Zeilberger's algorithm of creative telescoping in the most elementary manner.

## Setup

Our starting point is a repetition of [[Ne, Section 1]](#bib-Ne). For each integer $n = 0, 1, 2, \ldots$ define the rational function

$$R_n(t) := \left( \frac{(t-1) \cdots (t-n)}{t(t+1) \cdots (t+n)} \right)^2$$

and denote by $D_n$ the least common multiple of the numbers $1, 2, \ldots, n$ (with $D_0 = 1$ for completeness).

## Lemma 1 (Integer Structure of the Apéry Series) {#lemma-1}

**Statement.** There holds the equality

$$F_n := -\sum_{t=1}^\infty R_n'(t) = u_n \zeta(3) - v_n, \tag{1}$$

where $u_n \in \mathbb{Z}$, $D_n^3 v_n \in \mathbb{Z}$.

**Proof.** Taking the square of the partial-fraction expansion

$$\frac{(t-1) \cdots (t-n)}{t(t+1) \cdots (t+n)} = \sum_{k=0}^n \frac{(-1)^{n-k} \binom{n+k}{n} \binom{n}{k}}{t+k}$$

with the help of the relation

$$\frac{1}{t+k} \cdot \frac{1}{t+l} = \frac{1}{l-k} \cdot \left( \frac{1}{t+k} - \frac{1}{t+l} \right) \qquad \text{for } k \neq l,$$

we arrive at the formula

$$R_n(t) = \sum_{k=0}^n \left( \frac{A_{2k}^{(n)}}{(t+k)^2} + \frac{A_{1k}^{(n)}}{t+k} \right),$$

with $A_{jk} = A_{jk}^{(n)}$ satisfying the inclusions

$$A_{2k} = \binom{n+k}{n}^2 \binom{n}{k}^2 \in \mathbb{Z} \quad \text{and} \quad D_n A_{1k} \in \mathbb{Z}, \qquad k = 0, 1, \ldots, n. \tag{2}$$

Furthermore,

$$\sum_{k=0}^n A_{1k} = \sum_{k=0}^n \operatorname{Res}_{t=-k} R_n(t) = -\operatorname{Res}_{t=\infty} R_n(t) = 0$$

since $R_n(t) = O(t^{-2})$ as $t \to \infty$. Then $F_n$ takes the desired form $(1)$, with

$$u_n = 2 \sum_{k=0}^n A_{2k}, \qquad v_n = 2 \sum_{k=0}^n A_{2k} \sum_{l=1}^k \frac{1}{l^3} + \sum_{k=0}^n A_{1k} \sum_{l=1}^k \frac{1}{l^2}. \tag{3}$$

Finally, using the inclusions $(2)$ and the identity $D_n^j \cdot \sum_{l=1}^k \frac{1}{l^j} \in \mathbb{Z}$ for $j = 2, 3$, we deduce that $u_n \in \mathbb{Z}$ and $D_n^3 v_n \in \mathbb{Z}$ as required. $\square$

As a sanity check, since

$$R_0(t) = \frac{1}{t^2}, \qquad R_1(t) = \frac{1}{t^2} + \frac{4}{(t+1)^2} - \frac{4}{t} + \frac{4}{t+1},$$

formulas $(3)$ give $F_0 = 2\zeta(3)$ and $F_1 = 10\zeta(3) - 12$, in line with $(1)$.

Now, by Zeilberger's algorithm of creative telescoping [[PWZ, Chapter 6]](#bib-PWZ) we obtain the rational function $S_n(t) := s_n(t) R_n(t)$, where

$$s_n(t) := 4(2n+1) \bigl( -2t^2 + t + (2n+1)^2 \bigr), \tag{5}$$

with the following telescoping property.

## Lemma 2 (Creative Telescoping Identity) {#lemma-2}

**Statement.** For each $n = 1, 2, \ldots$, there holds the identity

$$(n+1)^3 R_{n+1}(t) - (2n+1)(17n^2 + 17n + 5) R_n(t) + n^3 R_{n-1}(t) = S_n(t+1) - S_n(t). \tag{6}$$

**"One-line" proof.** Divide both sides of $(6)$ by $R_n(t)$ and verify the resulting rational identity

$$\begin{aligned}
& (n+1)^3 \left( \frac{t-n-1}{t+n+1} \right)^2 - (2n+1)(17n^2 + 17n + 5) + n^3 \left( \frac{t+n}{t-n} \right)^2 \\
&\qquad = s_n(t+1) \left( \frac{t^2}{(t-n)(t+n+1)} \right)^2 - s_n(t),
\end{aligned}$$

where $s_n(t)$ is given by $(5)$. Both sides are rational functions of $t$ of bounded degree, so checking the equality at finitely many values (or, equivalently, clearing denominators and comparing polynomial coefficients) completes the verification. $\square$

## Remark (Why this lemma matters) {#why-lemma-2}

Identity $(6)$ is the engine that drives the rest of the proof. Summing both sides over $t = 1, 2, \ldots$, the right-hand side telescopes to $-S_n(1)$ (which turns out to vanish for $n \ge 1$, since $R_n(t)$ has a double zero at $t = 1$). What remains is the recurrence

$$(n+1)^3 F_{n+1} - (2n+1)(17n^2 + 17n + 5) F_n + n^3 F_{n-1} = 0,$$

which — applied to the integer and rational parts of $F_n = u_n \zeta(3) - v_n$ separately — yields the Apéry-style recursion recorded in [Lemma 3](#lemma-3) below. This is precisely the "magic" Apéry produced in 1978; here it falls out of Zeilberger's algorithm, and is verified by polynomial arithmetic.

## Lemma 3 (Recurrence for the Apéry Series) {#lemma-3}

**Statement.** The quantity $F_n$ defined in $(1)$ satisfies the difference equation

$$(n+1)^3 F_{n+1} - (2n+1)(17n^2 + 17n + 5) F_n + n^3 F_{n-1} = 0 \tag{7}$$

for $n = 1, 2, \ldots$.

**Idea of proof.** Apply the operator $-\dfrac{d}{dt}$ to identity $(6)$ of [Lemma 2](#lemma-2) and then sum over $t = 1, 2, \ldots$. Differentiation preserves the linear combination on the left, replacing each $R_m(t)$ by $R_m'(t)$, so by the definition $F_m = -\sum_{t=1}^\infty R_m'(t)$ the left-hand side becomes

$$(n+1)^3 F_{n+1} - (2n+1)(17n^2 + 17n + 5) F_n + n^3 F_{n-1}.$$

The right-hand side becomes the telescoping sum

$$-\sum_{t=1}^\infty \bigl( S_n'(t+1) - S_n'(t) \bigr) = S_n'(1) - \lim_{T \to \infty} S_n'(T),$$

and both terms vanish:

- *At $t = 1$.* For $n \ge 1$, the numerator of $R_n(t)$ contains the factor $(t-1)^2$, so $R_n$ has a double zero at $t = 1$. Since $S_n(t) = s_n(t) R_n(t)$ inherits that double zero, $S_n(1) = S_n'(1) = 0$.
- *At infinity.* From the explicit forms, $R_n(t) = O(t^{-2})$ and $s_n(t) = O(t^2)$, hence $S_n(t) = O(1)$ and $S_n'(t) = O(t^{-1}) \to 0$ as $t \to \infty$. (The same decay justifies interchanging the differentiation and the infinite summation in the first step.)

The right-hand side is therefore $0$, which is the recurrence $(7)$. $\square$

Consider another rational function

$$\widetilde R_n(t) := n!^2 (2t + n) \frac{(t-1) \cdots (t-n) \cdot (t+n+1) \cdots (t+2n)}{(t(t+1) \cdots (t+n))^4} \tag{8}$$

and the corresponding hypergeometric series

$$\widetilde F_n := \sum_{t=1}^\infty \widetilde R_n(t), \tag{9}$$

proposed by K. Ball.

## Lemma 4 (Ball's Analytic Bound) {#lemma-4}

**Statement.** For each $n = 0, 1, 2, \ldots$, there holds the inequality

$$0 < \widetilde F_n < 20(n+1)^4 (\sqrt{2} - 1)^{4n}. \tag{10}$$

For the rational function $(8)$ we obtain Zeilberger's certificate

$$
\begin{aligned}
\widetilde S_n(t) := {} & \frac{\widetilde R_n(t)}{(2t+n)(t+2n-1)(t+2n)} \cdot \bigl( -t^6 - (8n-1)t^5 + (4n^2 + 27n + 5) t^4 \\
& + 2n(67n^2 + 71n + 15) t^3 + (358n^4 + 339n^3 + 76n^2 - 7n - 3) t^2 \\
& + (384n^5 + 396n^4 + 97n^3 - 29n^2 - 17n - 2) t \\
& + n(153n^5 + 183n^4 + 50n^3 - 30n^2 - 22n - 4) \bigr).
\end{aligned} \tag{13}
$$

## Lemma 5 (Telescoping Identity for Ball's Series) {#lemma-5}

**Statement.** For each $n = 1, 2, \ldots$, there holds the identity

$$\begin{aligned}
(n+1)^3 \widetilde R_{n+1}(t) &- (2n+1)(17n^2 + 17n + 5) \widetilde R_n(t) + n^3 \widetilde R_{n-1}(t) \\
&= \widetilde S_n(t+1) - \widetilde S_n(t).
\end{aligned} \tag{14}$$

**"One-line" proof.** Divide both sides of $(14)$ by $\widetilde{R}_n(t)$ and verify the resulting rational identity — both sides are rational functions of $t$ of bounded degree. The left side is

$$(n+1)^3 \frac{\widetilde{R}_{n+1}(t)}{\widetilde{R}_n(t)} - (2n+1)(17n^2 + 17n + 5) + n^3 \frac{\widetilde{R}_{n-1}(t)}{\widetilde{R}_n(t)},$$

and the right side equals

$$\frac{\widetilde{S}_n(t+1)}{\widetilde{R}_n(t+1)} - \frac{\widetilde{S}_n(t)}{\widetilde{R}_n(t)},$$

where $\widetilde{S}_n(t)$ is the Zeilberger certificate given explicitly in equation $(13)$. Clearing denominators and comparing polynomial coefficients on both sides completes the verification. $\square$

## Remark (Parallelism with Apéry's telescoping) {#why-lemma-5}

Kuten identiteetti $(6)$ [Lemmasta 2](#lemma-2), myös yhtälö $(14)$ on teleskoopin identiteetti — avainmekanismi, joka muuntaa polynomiidentiteetin rekurrenssiksi. Summattaessa molemmat puolet yli $t = 1, 2, \ldots$, oikea puoli tulee teleskoopin summaksi. Kun $n \ge 1$, rationaalifunktio $\widetilde{R}_n(t)$ (määritelty kaavassa $(8)$) perii kaksoisnollan pisteessä $t = 1$ osoittajassa olevasta tulosta $(t-1) \cdots (t-n)$ ja sisältää tekijän $(2t+n)$. Nämä rakenteelliset nollat, yhdistettynä yhtälössä $(13)$ esitettyyn polynomiin, takaavat, että $\widetilde{S}_n(1)$ häviää ja $\widetilde{S}_n(t) \to 0$ kun $t \to \infty$, joten teleskoopin summa supistuu nollaksi. Jäljellä jää rekurrenssi

$$(n+1)^3 \widetilde{F}_{n+1} - (2n+1)(17n^2 + 17n + 5) \widetilde{F}_n + n^3 \widetilde{F}_{n-1} = 0,$$

joka on esitetty [Lemmassa 6](#lemma-6). Huomionarvoisesti tämä on *sama rekurrenssi* kuin [Lemman 3](#lemma-3) $F_n$:n toteuttama. Yhdistettynä täsmääviin alkuehtoisin ($F_0 = \widetilde{F}_0 = 2\zeta(3)$ ja $F_1 = \widetilde{F}_1 = 10\zeta(3) - 12$), tämä rekurrenssien yhtäsuuruus merkitsee, että nämä kaksi sarjaa ovat identtisiä — tämä on [Lemma 7](#lemma-7), todistuksen ratkaisevan tärkeä silta.

## Lemma 6 (Recurrence for Ball's Series) {#lemma-6}

**Statement.** The quantity $\widetilde F_n$ defined in $(9)$ satisfies the difference equation $(7)$ for $n = 1, 2, \ldots$.

## Lemma 7 (Coincidence of the Two Series) {#lemma-7}

**Statement.** For each $n = 0, 1, 2, \ldots$, the quantities $F_n$ and $\widetilde F_n$ defined in $(1)$ and $(9)$ coincide.

## Aside (Dependency Graph) {#dag}

The diagram below summarizes how the lemmas combine to give the theorem. Leaves (left column) are proved directly; the middle column derives recurrences from those leaves; [Lemma 7](#lemma-7) glues the two halves together; [Lemmas 1](#lemma-1), [4](#lemma-4), and [7](#lemma-7) feed the final contradiction. Each box is a link.

::: figure { intent="Dependency DAG of Apéry's theorem proof. Leaves Lemma 2 and Lemma 5 (telescoping identities) feed Lemma 3 and Lemma 6 (recurrences for F_n and tilde F_n respectively); together they yield Lemma 7 (F_n = tilde F_n). Lemma 1 (integer structure) and Lemma 4 (analytic bound) bypass Lemma 7 to feed the final theorem directly. Each node is a clickable link to its lemma." renderer=svg }
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 380" width="100%" style="max-width:720px;display:block;margin:0 auto;background:#fff;border:1px solid #d8d8d0;border-radius:4px;font-family:ui-serif,Georgia,serif">
    <defs>
      <marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#4a5566"/>
      </marker>
    </defs>

    <!-- edges -->
    <g stroke="#4a5566" stroke-width="1.5" fill="none" marker-end="url(#dag-arrow)">
      <!-- L2 -> L3 -->
      <line x1="160" y1="50"  x2="194" y2="50" />
      <!-- L5 -> L6 -->
      <line x1="160" y1="125" x2="194" y2="125" />
      <!-- L3 -> L7 -->
      <line x1="345" y1="50"  x2="379" y2="80" />
      <!-- L6 -> L7 -->
      <line x1="345" y1="125" x2="379" y2="94" />
      <!-- L7 -> Theorem -->
      <line x1="530" y1="87"  x2="557" y2="200" />
      <!-- L1 -> Theorem (straight) -->
      <line x1="160" y1="215" x2="557" y2="215" />
      <!-- L4 -> Theorem -->
      <line x1="160" y1="300" x2="557" y2="230" />
    </g>

    <!-- nodes -->
    <g font-size="13">
      <!-- Lemma 2 (leaf) -->
      <a href="#lemma-2"><g>
        <rect x="10" y="26" width="150" height="48" rx="6" ry="6" fill="#eef4fa" stroke="#5d7290" stroke-width="1.2"/>
        <text x="85" y="46" text-anchor="middle" font-weight="bold" fill="#2a4d7a">Lemma 2</text>
        <text x="85" y="64" text-anchor="middle" fill="#3a3a40" font-size="11">Apéry telescoping</text>
      </g></a>
      <!-- Lemma 5 (leaf) -->
      <a href="#lemma-5"><g>
        <rect x="10" y="101" width="150" height="48" rx="6" ry="6" fill="#eef4fa" stroke="#5d7290" stroke-width="1.2"/>
        <text x="85" y="121" text-anchor="middle" font-weight="bold" fill="#2a4d7a">Lemma 5</text>
        <text x="85" y="139" text-anchor="middle" fill="#3a3a40" font-size="11">Ball telescoping</text>
      </g></a>
      <!-- Lemma 1 (leaf) -->
      <a href="#lemma-1"><g>
        <rect x="10" y="191" width="150" height="48" rx="6" ry="6" fill="#eef4fa" stroke="#5d7290" stroke-width="1.2"/>
        <text x="85" y="211" text-anchor="middle" font-weight="bold" fill="#2a4d7a">Lemma 1</text>
        <text x="85" y="229" text-anchor="middle" fill="#3a3a40" font-size="11">integer structure of F_n</text>
      </g></a>
      <!-- Lemma 4 (leaf) -->
      <a href="#lemma-4"><g>
        <rect x="10" y="276" width="150" height="48" rx="6" ry="6" fill="#eef4fa" stroke="#5d7290" stroke-width="1.2"/>
        <text x="85" y="296" text-anchor="middle" font-weight="bold" fill="#2a4d7a">Lemma 4</text>
        <text x="85" y="314" text-anchor="middle" fill="#3a3a40" font-size="11">Ball's analytic bound</text>
      </g></a>
      <!-- Lemma 3 (derived) -->
      <a href="#lemma-3"><g>
        <rect x="195" y="26" width="150" height="48" rx="6" ry="6" fill="#faf5e8" stroke="#9a8550" stroke-width="1.2"/>
        <text x="270" y="46" text-anchor="middle" font-weight="bold" fill="#7a6020">Lemma 3</text>
        <text x="270" y="64" text-anchor="middle" fill="#3a3a40" font-size="11">recurrence for F_n</text>
      </g></a>
      <!-- Lemma 6 (derived) -->
      <a href="#lemma-6"><g>
        <rect x="195" y="101" width="150" height="48" rx="6" ry="6" fill="#faf5e8" stroke="#9a8550" stroke-width="1.2"/>
        <text x="270" y="121" text-anchor="middle" font-weight="bold" fill="#7a6020">Lemma 6</text>
        <text x="270" y="139" text-anchor="middle" fill="#3a3a40" font-size="11">recurrence for F̃_n</text>
      </g></a>
      <!-- Lemma 7 (derived) -->
      <a href="#lemma-7"><g>
        <rect x="380" y="63" width="150" height="48" rx="6" ry="6" fill="#faf5e8" stroke="#9a8550" stroke-width="1.2"/>
        <text x="455" y="83" text-anchor="middle" font-weight="bold" fill="#7a6020">Lemma 7</text>
        <text x="455" y="101" text-anchor="middle" fill="#3a3a40" font-size="11">F_n = F̃_n</text>
      </g></a>
      <!-- Theorem (final) -->
      <a href="#aperys-proof"><g>
        <rect x="558" y="191" width="155" height="48" rx="6" ry="6" fill="#e6f2dc" stroke="#3a6a30" stroke-width="1.8"/>
        <text x="635" y="211" text-anchor="middle" font-weight="bold" fill="#2a5a20">Apéry's Theorem</text>
        <text x="635" y="229" text-anchor="middle" fill="#3a3a40" font-size="11">ζ(3) is irrational</text>
      </g></a>
    </g>

    <!-- legend -->
    <g font-size="11" font-family="ui-sans-serif,system-ui,sans-serif">
      <rect x="12" y="350" width="14" height="14" rx="3" ry="3" fill="#eef4fa" stroke="#5d7290" stroke-width="1"/>
      <text x="32" y="361" fill="#444">leaf (proven directly)</text>
      <rect x="180" y="350" width="14" height="14" rx="3" ry="3" fill="#faf5e8" stroke="#9a8550" stroke-width="1"/>
      <text x="200" y="361" fill="#444">derived from earlier lemmas</text>
      <rect x="395" y="350" width="14" height="14" rx="3" ry="3" fill="#e6f2dc" stroke="#3a6a30" stroke-width="1.5"/>
      <text x="415" y="361" fill="#444">final result</text>
    </g>
  </svg>
:::

Read the chart left to right as the *logical* flow of the paper (not the order of presentation): polynomial identities → recurrences → equality of the two series → contradiction.

## Theorem (Apéry's Theorem — Proof) {#aperys-proof}

**Statement.** $\zeta(3)$ is irrational. (Restated.)

**Proof.** Suppose, on the contrary, that $\zeta(3) = p/q$ with $p, q$ positive integers. The strategy is to construct, for each $n$, the quantity $q D_n^3 F_n$ and show that the assumption forces it to be simultaneously a *positive integer* and *smaller than $1$* — an impossibility.

*Step 1 — it is an integer.* [Lemma 1](#lemma-1) provides the arithmetic structure $F_n = u_n \zeta(3) - v_n$ with $u_n \in \mathbb{Z}$ and $D_n^3 v_n \in \mathbb{Z}$. Substituting $\zeta(3) = p/q$ and multiplying by $q D_n^3$,

$$q D_n^3 F_n = D_n^3 u_n \cdot p - q \cdot D_n^3 v_n,$$

a difference of two integers, hence itself an integer.

*Step 2 — it is positive.* From [Lemma 4](#lemma-4) we have $\widetilde F_n > 0$; from [Lemma 7](#lemma-7), $F_n = \widetilde F_n$; therefore $F_n > 0$. Since $q, D_n > 0$ as well, $q D_n^3 F_n$ is a *positive* integer, so $q D_n^3 F_n \ge 1$.

*Step 3 — it is less than $1$ for large $n$.* The trivial bound $D_n < 3^n$ on the least common multiple, combined with the analytic estimate of [Lemma 4](#lemma-4) transported to $F_n$ via [Lemma 7](#lemma-7),

$$F_n = \widetilde F_n < 20(n+1)^4 (\sqrt 2 - 1)^{4n},$$

gives, for each $n = 0, 1, 2, \ldots$,

$$0 < q D_n^3 F_n < 20 q (n+1)^4 \cdot 3^{3n} (\sqrt{2} - 1)^{4n}. \tag{15}$$

The geometric factor at the heart of $(15)$ is

$$3^3 (\sqrt{2} - 1)^4 = 0.7948\ldots < 1,$$

so the right-hand side of $(15)$ decays geometrically in $n$ — only the polynomial factor $20 q (n+1)^4$ pushes against it, and the geometric decay wins. For $n$ sufficiently large the right-hand side is less than $1$.

*Conclusion.* For such $n$, Steps 1–3 together say $1 \le q D_n^3 F_n < 1$ — impossible. The assumption $\zeta(3) = p/q$ is false, and $\zeta(3)$ is irrational. $\square$

**Where the intermediate lemmas enter.** [Lemmas 2](#lemma-2) and [5](#lemma-5) are polynomial telescoping identities, verified mechanically via Zeilberger's certificates $s_n(t)$ and $\widetilde S_n(t)$. Summing each over $t = 1, 2, \ldots$ (the right-hand side telescopes to $0$ because the certificate vanishes at $t = 1$) produces the recurrences in [Lemmas 3](#lemma-3) and [6](#lemma-6). Those two recurrences are *identical*, and $F_n, \widetilde F_n$ share their first two values $F_0 = \widetilde F_0 = 2\zeta(3)$, $F_1 = \widetilde F_1 = 10\zeta(3) - 12$ — so by uniqueness of solutions to a second-order recursion, $F_n = \widetilde F_n$ for all $n$. That is [Lemma 7](#lemma-7), the bridge that lets Step 2 and the bound in Step 3 — both established for Ball's analytically tractable $\widetilde F_n$ — apply to the arithmetically tractable $F_n$.

## Remark

In spite of its elementary arguments, this proof of Apéry's theorem does not look simpler than the original (also elementary) Apéry's proof, well-explained in A. van der Poorten's informal report [[Po]](#bib-Po), or the (almost elementary) Beukers's proof [[Be]](#bib-Be) by means of Legendre polynomials and multiple integrals.

The fact that $\widetilde{F}_n = \widetilde{u}_n \zeta(3) - \widetilde{v}_n$ with $D_n \widetilde{u}_n, D_n^4 \widetilde{v}_n \in \mathbb{Z}$ was first discovered by K. Ball; the proof follows the lines of [Lemma 1](#lemma-1). An open question of T. Rivoal here is to get the better inclusions $\widetilde{u}_n, D_n^3 \widetilde{v}_n \in \mathbb{Z}$ by elementary means without going back to Apéry's series $(1)$.

## References

- **[[Ap]](#bib-Ap)** {#bib-Ap} R. Apéry. *Irrationalité de $\zeta(2)$ et $\zeta(3)$*. Astérisque **61** (1979), 11–13.
- **[[Ba]](#bib-Ba)** {#bib-Ba} W. N. Bailey. *Generalized hypergeometric series*. Cambridge Math. Tracts **32**, Cambridge Univ. Press, 1935. 2nd reprinted edition, Stechert-Hafner, New York–London, 1964.
- **[[BR]](#bib-BR)** {#bib-BR} K. Ball and T. Rivoal. *Irrationalité d'une infinité de valeurs de la fonction zêta aux entiers impairs*. Invent. Math. **146** (2001), 193–207.
- **[[Be]](#bib-Be)** {#bib-Be} F. Beukers. *A note on the irrationality of $\zeta(2)$ and $\zeta(3)$*. Bull. London Math. Soc. **11** (1979), 268–272.
- **[[Ne]](#bib-Ne)** {#bib-Ne} Yu. V. Nesterenko. *A few remarks on $\zeta(3)$*. Mat. Zametki **59** (1996), 865–880.
- **[[PWZ]](#bib-PWZ)** {#bib-PWZ} M. Petkovšek, H. S. Wilf, D. Zeilberger. *$A = B$*. A. K. Peters, 1997.
- **[[Po]](#bib-Po)** {#bib-Po} A. van der Poorten. *A proof that Euler missed... Apéry's proof of the irrationality of $\zeta(3)$*. Math. Intelligencer **1** (1978/79), 195–203.
- **[[Ri1]](#bib-Ri1)** {#bib-Ri1} T. Rivoal. arXiv preprint, 2000.
- **[[Ri2]](#bib-Ri2)** {#bib-Ri2} T. Rivoal. (Additional Rivoal references.)
- **[[Ri3]](#bib-Ri3)** {#bib-Ri3} T. Rivoal. (Additional Rivoal references.)
- **[[Zu1]](#bib-Zu1)** {#bib-Zu1} W. Zudilin. (2001 author preprints.)
- **[[Zu2]](#bib-Zu2)** {#bib-Zu2} W. Zudilin. (2001 author preprints.)
- **[[Zu3]](#bib-Zu3)** {#bib-Zu3} W. Zudilin. (2001 author preprints.)
- **[[Zu4]](#bib-Zu4)** {#bib-Zu4} W. Zudilin. (2001 author preprints.)

*Conversion note: this adaptive-markdown document was produced from `paper.tex` (AMS-TeX dialect). Headline result and Lemma 1 are converted in full; Lemma 2 is converted with its "one-line" proof; Lemmas 3–7 are present as statements only — ask the agent to expand any of their proofs from `paper.tex`. Bibliography entries are partially deferred.*
