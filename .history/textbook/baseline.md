---
doc_id: d-01KRJ4KNN0MQC2GA
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

- Explain the meaning of Rolle's theorem.
- Describe the significance of the Mean Value Theorem.
- State three important consequences of the Mean Value Theorem.

The Mean Value Theorem is one of the most important theorems in calculus. We look first at a special case, called Rolle's theorem, and then generalize.

## Theorem (Rolle's Theorem)

**Statement.** Let $f$ be a continuous function over the closed interval $[a,b]$ and differentiable over the open interval $(a,b)$ such that $f(a)=f(b)$. Then there exists at least one $c \in (a,b)$ such that $f'(c)=0$.

**Proof.** Let $k = f(a) = f(b)$. We consider three cases:

1. $f(x) = k$ for all $x \in (a,b)$.
2. There exists $x \in (a,b)$ such that $f(x) > k$.
3. There exists $x \in (a,b)$ such that $f(x) < k$.

*Case 1.* If $f(x) = k$ for all $x \in (a,b)$, then $f'(x) = 0$ for all $x \in (a,b)$.

*Case 2.* Since $f$ is a continuous function over the closed, bounded interval $[a,b]$, by the Extreme Value Theorem it attains an absolute maximum. Also, since there is a point $x \in (a,b)$ such that $f(x) > k$, the absolute maximum is greater than $k$. Therefore, the absolute maximum does not occur at either endpoint. As a result, the absolute maximum must occur at an interior point $c \in (a,b)$. Because $f$ has a maximum at an interior point $c$ and $f$ is differentiable at $c$, by Fermat's theorem, $f'(c) = 0$.

*Case 3.* The case when there exists a point $x \in (a,b)$ such that $f(x) < k$ is analogous to Case 2, with maximum replaced by minimum. $\square$

## Example (Using Rolle's Theorem)

For each of the following functions, verify that the function satisfies the criteria stated in Rolle's theorem and find all values $c$ in the given interval where $f'(c) = 0$.

1. $f(x) = x^2 + 2x$ over $[-2, 0]$
2. $f(x) = x^3 - 4x$ over $[-2, 2]$

**Solution.**

*Part a.* Since $f$ is a polynomial, it is continuous and differentiable everywhere. In addition, $f(-2) = 0 = f(0)$. Therefore, $f$ satisfies the criteria of Rolle's theorem. We conclude that there exists at least one value $c \in (-2, 0)$ such that $f'(c) = 0$. Since $f'(x) = 2x + 2 = 2(x+1)$, we see that $f'(c) = 2(c+1) = 0$ implies $c = -1$.

*Part b.* As in part a, $f$ is a polynomial and therefore is continuous and differentiable everywhere. Also, $f(-2) = 0 = f(2)$. That said, $f$ satisfies the criteria of Rolle's theorem. Differentiating, we find that $f'(x) = 3x^2 - 4$. Therefore, $f'(c) = 0$ when $c = \pm \dfrac{2}{\sqrt{3}}$. Both points are in the interval $[-2, 2]$, and therefore both points satisfy the conclusion of Rolle's theorem.

## Theorem (Mean Value Theorem)

**Statement.** Let $f$ be continuous over the closed interval $[a,b]$ and differentiable over the open interval $(a,b)$. Then there exists at least one point $c \in (a,b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

**Proof.** The proof follows from Rolle's theorem by introducing an appropriate function that satisfies the criteria of Rolle's theorem. Consider the line connecting $(a, f(a))$ and $(b, f(b))$. Since the slope of that line is

$$\frac{f(b) - f(a)}{b - a}$$

and the line passes through the point $(a, f(a))$, the equation of that line can be written as

$$y = \frac{f(b) - f(a)}{b - a}(x - a) + f(a).$$

Let $g(x)$ denote the vertical difference between the point $(x, f(x))$ and the point $(x, y)$ on that line. Therefore,

$$g(x) = f(x) - \left[ \frac{f(b) - f(a)}{b - a}(x - a) + f(a) \right].$$

Since the graph of $f$ intersects the secant line when $x = a$ and $x = b$, we see that $g(a) = 0 = g(b)$. Since $f$ is a differentiable function over $(a,b)$, $g$ is also a differentiable function over $(a,b)$. Furthermore, since $f$ is continuous over $[a,b]$, $g$ is also continuous over $[a,b]$. Therefore, $g$ satisfies the criteria of Rolle's theorem. Consequently, there exists a point $c \in (a,b)$ such that $g'(c) = 0$. Since

$$g'(x) = f'(x) - \frac{f(b) - f(a)}{b - a},$$

we see that

$$g'(c) = f'(c) - \frac{f(b) - f(a)}{b - a}.$$

Since $g'(c) = 0$, we conclude that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

$\square$

## Example (Verifying that the Mean Value Theorem Applies)

For $f(x) = \sqrt{x}$ over the interval $[0, 9]$, show that $f$ satisfies the hypothesis of the Mean Value Theorem, and therefore there exists at least one value $c \in (0, 9)$ such that $f'(c)$ is equal to the slope of the line connecting $(0, f(0))$ and $(9, f(9))$. Find these values $c$ guaranteed by the Mean Value Theorem.

**Solution.** We know that $f(x) = \sqrt{x}$ is continuous over $[0, 9]$ and differentiable over $(0, 9)$. Therefore, $f$ satisfies the hypotheses of the Mean Value Theorem, and there must exist at least one value $c \in (0, 9)$ such that $f'(c)$ is equal to the slope of the line connecting $(0, f(0))$ and $(9, f(9))$.

To determine which value(s) of $c$ are guaranteed, first calculate the derivative of $f$. The derivative is $f'(x) = \dfrac{1}{2\sqrt{x}}$. The slope of the line connecting $(0, f(0))$ and $(9, f(9))$ is given by

$$\frac{f(9) - f(0)}{9 - 0} = \frac{\sqrt{9} - \sqrt{0}}{9 - 0} = \frac{3}{9} = \frac{1}{3}.$$

We want to find $c$ such that $f'(c) = \dfrac{1}{3}$. That is, we want to find $c$ such that

$$\frac{1}{2\sqrt{c}} = \frac{1}{3}.$$

Solving this equation for $c$, we obtain $c = \dfrac{9}{4}$. At this point, the slope of the tangent line equals the slope of the line joining the endpoints.

## Example (Mean Value Theorem and Velocity)

If a rock is dropped from a height of $100$ ft, its position $t$ seconds after it is dropped until it hits the ground is given by the function $s(t) = -16t^2 + 100$.

1. Determine how long it takes before the rock hits the ground.
2. Find the average velocity $v_{\text{avg}}$ of the rock from when the rock is released until the rock hits the ground.
3. Find the time $t$ guaranteed by the Mean Value Theorem when the instantaneous velocity of the rock is $v_{\text{avg}}$.

**Solution.**

*Part a.* When the rock hits the ground, its position is $s(t) = 0$. Solving the equation $-16t^2 + 100 = 0$ for $t$, we find that $t = \pm \dfrac{5}{2}$ sec. Since we are only considering $t \geq 0$, the ball will hit the ground $\dfrac{5}{2}$ sec after it is dropped.

*Part b.* The average velocity is given by

$$v_{\text{avg}} = \frac{s(5/2) - s(0)}{5/2 - 0} = \frac{0 - 100}{5/2} = -40 \text{ ft/sec}.$$

*Part c.* The instantaneous velocity is given by the derivative of the position function. Therefore, we need to find a time $t$ such that $v(t) = s'(t) = v_{\text{avg}} = -40$ ft/sec. Since $s(t)$ is continuous over the interval $[0, 5/2]$ and differentiable over the interval $(0, 5/2)$, by the Mean Value Theorem, there is guaranteed to be a point $c \in (0, 5/2)$ such that

$$s'(c) = \frac{s(5/2) - s(0)}{5/2 - 0} = -40.$$

Taking the derivative of the position function $s(t)$, we find that $s'(t) = -32t$. Therefore, the equation reduces to $s'(c) = -32c = -40$. Solving this equation for $c$, we have $c = \dfrac{5}{4}$. Therefore, $\dfrac{5}{4}$ sec after the rock is dropped, the instantaneous velocity equals the average velocity of the rock during its free fall: $-40$ ft/sec.

## Corollaries of the Mean Value Theorem

### Corollary 1 (Functions with a Derivative of Zero)

**Statement.** Let $f$ be differentiable over an interval $I$. If $f'(x) = 0$ for all $x \in I$, then $f(x) = $ constant for all $x \in I$.

**Proof.** Since $f$ is differentiable over $I$, $f$ must be continuous over $I$. Suppose $f(x)$ is not constant for all $x$ in $I$. Then there exist $a, b \in I$, where $a \neq b$ and $f(a) \neq f(b)$. Choose the notation so that $a < b$. Therefore,

$$\frac{f(b) - f(a)}{b - a} \neq 0.$$

Since $f$ is a differentiable function, by the Mean Value Theorem, there exists $c \in (a, b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

Therefore, there exists $c \in I$ such that $f'(c) \neq 0$, which contradicts the assumption that $f'(x) = 0$ for all $x \in I$. $\square$

### Corollary 2 (Constant Difference Theorem)

**Statement.** If $f$ and $g$ are differentiable over an interval $I$ and $f'(x) = g'(x)$ for all $x \in I$, then $f(x) = g(x) + C$ for some constant $C$.

**Proof.** Let $h(x) = f(x) - g(x)$. Then $h'(x) = f'(x) - g'(x) = 0$ for all $x \in I$. By Corollary 1, there is a constant $C$ such that $h(x) = C$ for all $x \in I$. Therefore, $f(x) = g(x) + C$ for all $x \in I$. $\square$

### Corollary 3 (Increasing and Decreasing Functions)

**Statement.** Let $f$ be continuous over the closed interval $[a, b]$ and differentiable over the open interval $(a, b)$.

1. If $f'(x) > 0$ for all $x \in (a, b)$, then $f$ is an increasing function over $[a, b]$.
2. If $f'(x) < 0$ for all $x \in (a, b)$, then $f$ is a decreasing function over $[a, b]$.

**Proof.** We will prove (i); the proof of (ii) is similar. Suppose $f$ is not an increasing function on $I$. Then there exist $a$ and $b$ in $I$ such that $a < b$, but $f(a) \geq f(b)$. Since $f$ is a differentiable function over $I$, by the Mean Value Theorem there exists $c \in (a, b)$ such that

$$f'(c) = \frac{f(b) - f(a)}{b - a}.$$

Since $f(a) \geq f(b)$, we know that $f(b) - f(a) \leq 0$. Also, $a < b$ tells us that $b - a > 0$. We conclude that

$$f'(c) = \frac{f(b) - f(a)}{b - a} \leq 0.$$

However, $f'(x) > 0$ for all $x \in I$. This is a contradiction, and therefore $f$ must be an increasing function over $I$. $\square$

## Glossary

**Mean Value Theorem.** If $f$ is continuous over $[a, b]$ and differentiable over $(a, b)$, then there exists $c \in (a, b)$ such that $f'(c) = \dfrac{f(b) - f(a)}{b - a}$.

**Rolle's Theorem.** If $f$ is continuous over $[a, b]$ and differentiable over $(a, b)$, and if $f(a) = f(b)$, then there exists $c \in (a, b)$ such that $f'(c) = 0$.
