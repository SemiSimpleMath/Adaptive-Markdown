---
source_url: https://math.libretexts.org/Bookshelves/Calculus/Calculus_(OpenStax)/04%3A_Applications_of_Derivatives/4.04%3A_The_Mean_Value_Theorem
title: The Mean Value Theorem
source: LibreTexts (Calculus, OpenStax) — Gilbert Strang & Edwin "Jed" Herman
fetched: 2026-05-12
license: CC BY-NC-SA 4.0
audience: novice
language: en
---

# The Mean Value Theorem

## Learning Objectives

- Selitä Rollen lauseen merkitys.
- اوصف الأهمية متاع مبرهنة القيمة المتوسطة.
- Énoncer trois conséquences importantes du théorème des accroissements finis.

Le théorème des accroissements finis est l'un des théorèmes les plus importants du calcul différentiel. Nous commençons par étudier un cas particulier, appelé théorème de Rolle, puis nous le généralisons.

## Theorem (Rolle's Theorem) {#rolle}

**Statement.** Let $f$ be a continuous function over the closed interval $[a,b]$ and differentiable over the open interval $(a,b)$ such that $f(a)=f(b)$. Then there exists at least one $c \in (a,b)$ such that $f'(c)=0$.

::: figure { intent="Animation of Rolle's theorem: a smooth curve on [a,b] with f(a)=f(b) shown as a dashed reference line; a moving point sweeps the curve while its tangent line rotates; the tangent turns green and the motion slows when the tangent is horizontal — that point is c, where f'(c)=0." renderer=canvas }
<canvas id="rolle-stmt-anim" width="640" height="320" style="display:block;margin:0 auto;background:#fff;border:1px solid #d8d8d0;border-radius:4px"></canvas>
  <script>
  (function() {
    const cv  = document.getElementById('rolle-stmt-anim');
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height;
    // f(x) = (x-1)(x-3) + 1 on [0,4]: f(0) = f(4) = 4, min at x = 2 with f(2) = 0
    const f  = x => (x - 1) * (x - 3) + 1;
    const df = x => 2 * x - 4;
    const xMin = 0, xMax = 4;
    const yMin = -0.5, yMax = 4.8;
    const padL = 50, padR = 20, padT = 30, padB = 44;
    const xToPx = x => padL + (W - padL - padR) * (x - xMin) / (xMax - xMin);
    const yToPx = y => H - padB - (H - padT - padB) * (y - yMin) / (yMax - yMin);

    let t = 0;
    function frame() {
      ctx.clearRect(0, 0, W, H);

      // axes
      ctx.strokeStyle = '#bbb'; ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, H - padB); ctx.lineTo(W - padR, H - padB);
      ctx.moveTo(padL, padT);     ctx.lineTo(padL, H - padB);
      ctx.stroke();

      // a, b labels on x-axis
      ctx.fillStyle = '#444'; ctx.font = '14px ui-serif, Georgia, serif';
      ctx.fillText('a', xToPx(0) - 4, H - padB + 18);
      ctx.fillText('b', xToPx(4) - 4, H - padB + 18);

      // f(a) = f(b) dashed reference line
      ctx.strokeStyle = '#aaa'; ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(xToPx(0), yToPx(f(0)));
      ctx.lineTo(xToPx(4), yToPx(f(0)));
      ctx.stroke();
      ctx.setLineDash([]);

      // endpoint dots and label
      ctx.fillStyle = '#444';
      ctx.beginPath(); ctx.arc(xToPx(0), yToPx(f(0)), 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.beginPath(); ctx.arc(xToPx(4), yToPx(f(4)), 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.font = '13px ui-serif, Georgia, serif';
      ctx.fillText('f(a) = f(b)', xToPx(4) - 88, yToPx(f(0)) - 8);

      // curve
      ctx.strokeStyle = '#2a4d7a'; ctx.lineWidth = 2;
      ctx.beginPath();
      const N = 240;
      for (let i = 0; i <= N; i++) {
        const x = xMin + (xMax - xMin) * i / N;
        const px = xToPx(x), py = yToPx(f(x));
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.stroke();

      // moving point — sweep through (a, b) using a sinusoid
      const x0 = 2 + 1.9 * Math.sin(t);
      const slope = df(x0);
      const px = xToPx(x0), py = yToPx(f(x0));
      const isFlat = Math.abs(slope) < 0.07;

      // tangent line — direction in screen pixels accounts for axis scaling
      const pxPerX = (W - padL - padR) / (xMax - xMin);
      const pxPerY = (H - padT - padB) / (yMax - yMin);
      const dxPx = 1;
      const dyPx = -slope * (pxPerY / pxPerX);
      const norm = Math.hypot(dxPx, dyPx);
      const ux = dxPx / norm, uy = dyPx / norm;
      const segPx = 80;
      ctx.strokeStyle = isFlat ? '#3ab83a' : (slope > 0 ? '#2a4d7a' : '#b34141');
      ctx.lineWidth = isFlat ? 3 : 2;
      ctx.beginPath();
      ctx.moveTo(px - segPx * ux, py - segPx * uy);
      ctx.lineTo(px + segPx * ux, py + segPx * uy);
      ctx.stroke();

      // moving point
      ctx.fillStyle = '#222';
      ctx.beginPath(); ctx.arc(px, py, 4.5, 0, 2 * Math.PI); ctx.fill();

      // c marker on x-axis and annotation when tangent is flat
      if (isFlat) {
        ctx.strokeStyle = '#3ab83a'; ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, py); ctx.lineTo(px, H - padB);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#3ab83a';
        ctx.font = 'bold 14px ui-serif, Georgia, serif';
        ctx.fillText('c', px - 4, H - padB + 18);
        ctx.fillText("f'(c) = 0", px + 12, py - 10);
      }

      // numeric read-out
      ctx.fillStyle = '#444'; ctx.font = '13px ui-monospace, monospace';
      ctx.fillText("f'(x) = " + slope.toFixed(2), W - 140, padT - 10);

      // slow down near the flat point so the eye catches it
      t += isFlat ? 0.006 : 0.018;
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  })();
  </script>
:::

**In plain English.** Imagine you draw a smooth, unbroken curve on a piece of paper — no jumps, no sharp corners. Suppose the curve starts and ends at exactly the same height. Then somewhere in the middle, the curve has to be *perfectly flat* for an instant: like the top of a hill or the bottom of a valley.

Think about it like throwing a ball straight up. It starts at your hand and ends back at your hand — same height. While it's in the air, it has to slow down, stop for a split second at the top, and come back down. That split second when it's not moving — that's the "flat" point Rolle's theorem promises.

Hienojen sanojen merkitys:
- **jatkuva** = käyrässä ei ole katkoja (voit piirtää sen nostamatta kynää paperista),
- **derivoituva** = käyrässä ei ole teräviä kulmia (se on sileä),
- **$f(a) = f(b)$** = sama korkeus alussa ja lopussa,
- **$f'(c) = 0$** = käyrä on vaakasuorassa pisteessä $c$ (sen tangentti on vaakasuora).

**Proof.** Let $k = f(a) = f(b)$. We consider three cases:

1. $f(x) = k$ for all $x \in (a,b)$.
2. There exists $x \in (a,b)$ such that $f(x) > k$.
3. There exists $x \in (a,b)$ such that $f(x) < k$.

*Case 1.* If $f(x) = k$ for all $x \in (a,b)$, then $f'(x) = 0$ for all $x \in (a,b)$.

*Case 2.* Since $f$ is a continuous function over the closed, bounded interval $[a,b]$, by the Extreme Value Theorem it attains an absolute maximum. Also, since there is a point $x \in (a,b)$ such that $f(x) > k$, the absolute maximum is greater than $k$. Therefore, the absolute maximum does not occur at either endpoint. As a result, the absolute maximum must occur at an interior point $c \in (a,b)$. Because $f$ has a maximum at an interior point $c$ and $f$ is differentiable at $c$, by Fermat's theorem, $f'(c) = 0$.

*Case 3.* The case when there exists a point $x \in (a,b)$ such that $f(x) < k$ is analogous to Case 2, with maximum replaced by minimum. $\square$

## Note (My study notes) {#my-notes}

1. I need to study this section today.
2. The TA said he would go over it tomorrow.

## Example (Using Rolle's Theorem) {#ex-rolle}

For each of the following functions, verify that the function satisfies the criteria stated in Rolle's theorem and find all values $c$ in the given interval where $f'(c) = 0$.

1. $f(x) = x^2 + 2x$ over $[-2, 0]$
2. $f(x) = x^3 - 4x$ over $[-2, 2]$

**Solution.**

*Part a.* Since $f$ is a polynomial, it is continuous and differentiable everywhere. In addition, $f(-2) = 0 = f(0)$. Therefore, $f$ satisfies the criteria of Rolle's theorem. We conclude that there exists at least one value $c \in (-2, 0)$ such that $f'(c) = 0$. Since $f'(x) = 2x + 2 = 2(x+1)$, we see that $f'(c) = 2(c+1) = 0$ implies $c = -1$.

*Part b.* As in part a, $f$ is a polynomial and therefore is continuous and differentiable everywhere. Also, $f(-2) = 0 = f(2)$. That said, $f$ satisfies the criteria of Rolle's theorem. Differentiating, we find that $f'(x) = 3x^2 - 4$. Therefore, $f'(c) = 0$ when $c = \pm \dfrac{2}{\sqrt{3}}$. Both points are in the interval $[-2, 2]$, and therefore both points satisfy the conclusion of Rolle's theorem.

## Theorem (Mean Value Theorem) {#mvt}

**Statement.** Let $f$ be continuous over the closed interval $[a,b]$ and differentiable over the open interval $(a,b)$. Then there exists at least one point $c \in (a,b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

**Proof.** The proof follows from [Rolle's theorem](#rolle) by introducing an appropriate function that satisfies its criteria. Consider the line connecting $(a, f(a))$ and $(b, f(b))$. Since the slope of that line is

$$\frac{f(b) - f(a)}{b - a}$$

and the line passes through the point $(a, f(a))$, the equation of that line can be written as

$$y = \frac{f(b) - f(a)}{b - a}(x - a) + f(a).$$

Let $g(x)$ denote the vertical difference between the point $(x, f(x))$ and the point $(x, y)$ on that line. Therefore,

$$g(x) = f(x) - \left[ \frac{f(b) - f(a)}{b - a}(x - a) + f(a) \right].$$

Since the graph of $f$ intersects the secant line when $x = a$ and $x = b$, we see that $g(a) = 0 = g(b)$. Since $f$ is a differentiable function over $(a,b)$, $g$ is also a differentiable function over $(a,b)$. Furthermore, since $f$ is continuous over $[a,b]$, $g$ is also continuous over $[a,b]$. Therefore, $g$ satisfies the criteria of [Rolle's theorem](#rolle). Consequently, there exists a point $c \in (a,b)$ such that $g'(c) = 0$. Since

$$g'(x) = f'(x) - \frac{f(b) - f(a)}{b - a},$$

we see that

$$g'(c) = f'(c) - \frac{f(b) - f(a)}{b - a}.$$

Since $g'(c) = 0$, we conclude that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

$\square$

## Example (Verifying that the Mean Value Theorem Applies) {#ex-mvt-1}

For $f(x) = \sqrt{x}$ over the interval $[0, 9]$, show that $f$ satisfies the hypothesis of the Mean Value Theorem, and therefore there exists at least one value $c \in (0, 9)$ such that $f'(c)$ is equal to the slope of the line connecting $(0, f(0))$ and $(9, f(9))$. Find these values $c$ guaranteed by the Mean Value Theorem.

**Solution.** We know that $f(x) = \sqrt{x}$ is continuous over $[0, 9]$ and differentiable over $(0, 9)$. Therefore, $f$ satisfies the hypotheses of the [Mean Value Theorem](#mvt), and there must exist at least one value $c \in (0, 9)$ such that $f'(c)$ is equal to the slope of the line connecting $(0, f(0))$ and $(9, f(9))$.

To determine which value(s) of $c$ are guaranteed, first calculate the derivative of $f$. The derivative is $f'(x) = \dfrac{1}{2\sqrt{x}}$. The slope of the line connecting $(0, f(0))$ and $(9, f(9))$ is given by

$$\frac{f(9) - f(0)}{9 - 0} = \frac{\sqrt{9} - \sqrt{0}}{9 - 0} = \frac{3}{9} = \frac{1}{3}.$$

We want to find $c$ such that $f'(c) = \dfrac{1}{3}$. That is, we want to find $c$ such that

$$\frac{1}{2\sqrt{c}} = \frac{1}{3}.$$

Solving this equation for $c$, we obtain $c = \dfrac{9}{4}$. At this point, the slope of the tangent line equals the slope of the line joining the endpoints.

## Example (Mean Value Theorem and Velocity) {#ex-mvt-2}

If a rock is dropped from a height of $100$ ft, its position $t$ seconds after it is dropped until it hits the ground is given by the function $s(t) = -16t^2 + 100$.

1. Determine how long it takes before the rock hits the ground.
2. Find the average velocity $v_{\text{avg}}$ of the rock from when the rock is released until the rock hits the ground.
3. Find the time $t$ guaranteed by the Mean Value Theorem when the instantaneous velocity of the rock is $v_{\text{avg}}$.

**Solution.**

*Part a.* When the rock hits the ground, its position is $s(t) = 0$. Solving the equation $-16t^2 + 100 = 0$ for $t$, we find that $t = \pm \dfrac{5}{2}$ sec. Since we are only considering $t \geq 0$, the ball will hit the ground $\dfrac{5}{2}$ sec after it is dropped.

*Part b.* The average velocity is given by

$$v_{\text{avg}} = \frac{s(5/2) - s(0)}{5/2 - 0} = \frac{0 - 100}{5/2} = -40 \text{ ft/sec}.$$

*Part c.* The instantaneous velocity is given by the derivative of the position function. Therefore, we need to find a time $t$ such that $v(t) = s'(t) = v_{\text{avg}} = -40$ ft/sec. Since $s(t)$ is continuous over the interval $[0, 5/2]$ and differentiable over the interval $(0, 5/2)$, by the [Mean Value Theorem](#mvt), there is guaranteed to be a point $c \in (0, 5/2)$ such that

$$s'(c) = \frac{s(5/2) - s(0)}{5/2 - 0} = -40.$$

Taking the derivative of the position function $s(t)$, we find that $s'(t) = -32t$. Therefore, the equation reduces to $s'(c) = -32c = -40$. Solving this equation for $c$, we have $c = \dfrac{5}{4}$. Therefore, $\dfrac{5}{4}$ sec after the rock is dropped, the instantaneous velocity equals the average velocity of the rock during its free fall: $-40$ ft/sec.

## Corollaries of the Mean Value Theorem

### Corollary 1 (Functions with a Derivative of Zero) {#mvt-cor-1}

**Statement.** Let $f$ be differentiable over an interval $I$. If $f'(x) = 0$ for all $x \in I$, then $f(x) = $ constant for all $x \in I$.

**Proof.** Since $f$ is differentiable over $I$, $f$ must be continuous over $I$. Suppose $f(x)$ is not constant for all $x$ in $I$. Then there exist $a, b \in I$, where $a \neq b$ and $f(a) \neq f(b)$. Choose the notation so that $a < b$. Therefore,

$$\frac{f(b) - f(a)}{b - a} \neq 0.$$

Since $f$ is a differentiable function, by the [Mean Value Theorem](#mvt), there exists $c \in (a, b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

Therefore, there exists $c \in I$ such that $f'(c) \neq 0$, which contradicts the assumption that $f'(x) = 0$ for all $x \in I$. $\square$

### Corollary 2 (Constant Difference Theorem) {#mvt-cor-2}

**Statement.** If $f$ and $g$ are differentiable over an interval $I$ and $f'(x) = g'(x)$ for all $x \in I$, then $f(x) = g(x) + C$ for some constant $C$.

**Proof.** Let $h(x) = f(x) - g(x)$. Then $h'(x) = f'(x) - g'(x) = 0$ for all $x \in I$. By [Corollary 1](#mvt-cor-1), there is a constant $C$ such that $h(x) = C$ for all $x \in I$. Therefore, $f(x) = g(x) + C$ for all $x \in I$. $\square$

### Corollary 3 (Increasing and Decreasing Functions) {#mvt-cor-3}

**Statement.** Let $f$ be continuous over the closed interval $[a, b]$ and differentiable over the open interval $(a, b)$.

1. If $f'(x) > 0$ for all $x \in (a, b)$, then $f$ is an increasing function over $[a, b]$.
2. If $f'(x) < 0$ for all $x \in (a, b)$, then $f$ is a decreasing function over $[a, b]$.

**Proof.** We will prove (i); the proof of (ii) is similar. Suppose $f$ is not an increasing function on $I$. Then there exist $a$ and $b$ in $I$ such that $a < b$, but $f(a) \geq f(b)$. Since $f$ is a differentiable function over $I$, by the [Mean Value Theorem](#mvt) there exists $c \in (a, b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

Since $f(a) \geq f(b)$, we know that $f(b) - f(a) \leq 0$. Also, $a < b$ tells us that $b - a > 0$. We conclude that

$$f'(c) = \frac{f(b) - f(a)}{b - a} \leq 0.$$

However, $f'(x) > 0$ for all $x \in I$. This is a contradiction, and therefore $f$ must be an increasing function over $I$. $\square$

## Glossary

**[Mean Value Theorem](#mvt).** If $f$ is continuous over $[a, b]$ and differentiable over $(a, b)$, then there exists $c \in (a, b)$ such that $f'(c) = \dfrac{f(b) - f(a)}{b - a}$.

**[Rolle's Theorem](#rolle).** If $f$ is continuous over $[a, b]$ and differentiable over $(a, b)$, and if $f(a) = f(b)$, then there exists $c \in (a, b)$ such that $f'(c) = 0$.
