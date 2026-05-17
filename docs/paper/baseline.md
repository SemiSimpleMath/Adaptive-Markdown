---
doc_id: d-01KRJ4KNN0TEDGCW
source_url: https://arxiv.org/abs/math/0202159
title: An Elementary Proof of Apéry's Theorem
authors: ["Wadim Zudilin (Moscow Lomonosov State University)"]
fetched: 2026-05-12
license: arXiv non-exclusive license to distribute
audience: expert
language: en
---

# An Elementary Proof of Apéry's Theorem

**Authors:** Wadim Zudilin (Moscow Lomonosov State University)

**arXiv:** [math/0202159](https://arxiv.org/abs/math/0202159) (17 February 2002)

**Citation:** Zudilin, W. *An elementary proof of Apéry's theorem.* E-print arXiv:math/0202159, 2002.

## Abstract

We present a new "elementary" proof of the irrationality of $\zeta(3)$ based on some recent "hypergeometric" ideas of Yu. Nesterenko, T. Rivoal, and K. Ball, and on Zeilberger's algorithm of creative telescoping.

## Introduction

<!-- id:b-01KRPDF0XZAYPY2W -->
A question of an arithmetic nature of the values of Riemann's zeta function

$$\zeta(s) := \sum_{n=1}^{\infty} \frac{1}{n^s}$$

at odd integral points $s = 3, 5, 7, \ldots$ looks like a challenge for Number Theory. An expected answer "each odd zeta value is transcendental" is still far from being proved. We dispose of a particular information on the irrationality of odd zeta values, namely:

- $\zeta(3)$ is irrational (R. Apéry, 1978);
- infinitely many of the numbers $\zeta(3), \zeta(5), \zeta(7), \ldots$ are irrational (T. Rivoal, 2000);
- each set $\zeta(s+2), \zeta(s+4), \ldots, \zeta(8s-3), \zeta(8s-1)$ with odd $s > 1$ contains at least one irrational number (Zudilin, 2001);
- at least one of the four numbers $\zeta(5), \zeta(7), \zeta(9), \zeta(11)$ is irrational (Zudilin, 2001).

<!-- id:b-01KRPDF0YNPD9NCR -->
All these results have a classical well-poised-hypergeometric origin. The aim of this note is to prove Apéry's famous result by "elementary means."

## Definitions and Setup

For each integer $n = 0, 1, 2, \ldots$ define the rational function

$$R_n(t) := \left( \frac{(t-1)(t-2)\cdots(t-n)}{t(t+1)\cdots(t+n)} \right)^2$$

and denote by $D_n$ the least common multiple of the numbers $1, 2, \ldots, n$ (and $D_0 = 1$ for completeness).

## Lemma 1 (Integer Structure of the Apéry Series)

**Statement.** There holds the equality

$$F_n := -\sum_{t=1}^{\infty} R_n'(t) = u_n \zeta(3) - v_n, \tag{1}$$

where $u_n \in \mathbb{Z}$ and $D_n^3 v_n \in \mathbb{Z}$.

**Proof.** Taking the square of the partial-fraction expansion

$$\frac{(t-1)\cdots(t-n)}{t(t+1)\cdots(t+n)} = \sum_{k=0}^{n} \frac{(-1)^{n-k} \binom{n+k}{n} \binom{n}{k}}{t+k}$$

with the help of the relation

$$\frac{1}{t+k} \cdot \frac{1}{t+l} = \frac{1}{l-k}\left( \frac{1}{t+k} - \frac{1}{t+l} \right) \quad \text{for } k \neq l,$$

we arrive at the formula

$$R_n(t) = \sum_{k=0}^{n} \left( \frac{A_{2k}^{(n)}}{(t+k)^2} + \frac{A_{1k}^{(n)}}{t+k} \right),$$

with $A_{jk} = A_{jk}^{(n)}$ satisfying the inclusions

$$A_{2k} = \binom{n+k}{k}^2 \binom{n}{k}^2 \in \mathbb{Z} \quad \text{and} \quad D_n A_{1k} \in \mathbb{Z}, \quad k = 0, 1, \ldots, n. \tag{2}$$

Furthermore,

$$\sum_{k=0}^{n} A_{1k} = \sum_{k=0}^{n} \operatorname{Res}_{t=-k} R_n(t) = -\operatorname{Res}_{t=\infty} R_n(t) = 0$$

since $R_n(t) = O(t^{-2})$ as $t \to \infty$, hence the quantity

$$F_n = -\sum_{t=1}^{\infty} \sum_{k=0}^{n} \left( \frac{2 A_{2k}}{(t+k)^3} + \frac{A_{1k}}{(t+k)^2} \right) = \sum_{k=0}^{n} \sum_{l=k+1}^{\infty} \left( \frac{2 A_{2k}}{l^3} + \frac{A_{1k}}{l^2} \right)$$

$$= 2 \sum_{k=0}^{n} A_{2k} \left( \zeta(3) - \sum_{l=1}^{k} \frac{1}{l^3} \right) + \sum_{k=0}^{n} A_{1k} \left( \zeta(2) - \sum_{l=1}^{k} \frac{1}{l^2} \right)$$

has the desired form (1), with

$$u_n = 2 \sum_{k=0}^{n} A_{2k}, \qquad v_n = 2 \sum_{k=0}^{n} A_{2k} \sum_{l=1}^{k} \frac{1}{l^3} + \sum_{k=0}^{n} A_{1k} \sum_{l=1}^{k} \frac{1}{l^2}. \tag{3}$$

Finally, using the inclusions (2) and

$$D_n^j \cdot \sum_{l=1}^{k} \frac{1}{l^j} \in \mathbb{Z} \quad \text{for } k = 0, 1, \ldots, n, \; j = 2, 3,$$

we deduce that $u_n \in \mathbb{Z}$ and $D_n^3 v_n \in \mathbb{Z}$ as required. $\square$

Since

$$R_0(t) = \frac{1}{t^2}, \qquad R_1(t) = \frac{1}{t^2} + \frac{1}{(t+1)^2} - \frac{4}{t} + \frac{4}{t+1},$$

in accordance with formulae (3) we find that

$$F_0 = 2\zeta(3) \quad \text{and} \quad F_1 = 10\zeta(3) - 12. \tag{4}$$

## Lemma 4 (Ball's Asymptotic Bound)

Consider the rational function

$$\tilde{R}_n(t) := n!^2 (2t + n) \cdot \frac{(t-1)\cdots(t-n) \cdot (t+n+1)\cdots(t+2n)}{(t(t+1)\cdots(t+n))^4} \tag{8}$$

and the corresponding hypergeometric series

$$\tilde{F}_n := \sum_{t=1}^{\infty} \tilde{R}_n(t), \tag{9}$$

proposed by K. Ball.

**Statement.** For each $n = 0, 1, 2, \ldots$, there holds the inequality

$$0 < \tilde{F}_n < 20(n+1)^4 (\sqrt{2} - 1)^{4n}. \tag{10}$$

**Proof.** Since $\tilde{R}_n(t) = 0$ for $t = 1, 2, \ldots, n$ and $\tilde{R}_n(t) > 0$ for $t > n$, we deduce that $\tilde{F}_n > 0$.

With the help of the elementary inequality

$$\frac{1}{m} \cdot \frac{(m+1)^m}{m^{m-1}} = \left(1 + \frac{1}{m}\right)^m < e < \left(1 + \frac{1}{m}\right)^{m+1} = \frac{1}{m} \cdot \frac{(m+1)^{m+1}}{m^m}$$

that yields $(m+1)^m / m^{m-1} < e^m < (m+1)^{m+1}/m^m$ for $m = 1, 2, \ldots$, we deduce that

$$\frac{e^{-n} (m+n)^{m+n-1}}{m^{m-1}} < m(m+1)\cdots(m+n-1) < \frac{e^{-n} (m+n)^{m+n}}{m^m}.$$

After applying this bound, defining

$$f(\xi) := \log\frac{\xi^5 (\xi+2)^{\xi+2}}{(\xi-1)^{\xi-1}(\xi+1)^{5(\xi+1)}},$$

one finds that the unique real solution $\xi_0$ of $f'(\xi) = 0$ in the region $\xi > 1$ is the zero of

$$\xi^5(\xi+2) - (\xi-1)(\xi+1)^5 = -\xi^4 + \tfrac{1}{2}\xi^2 - 5\xi + \tfrac{1}{2} \cdot \tfrac{1}{2} \cdot \tfrac{7}{8},$$

determined explicitly by $\xi_0 = -\tfrac{1}{2} + \tfrac{\sqrt{5}}{4} + \cdots$. Thus

$$\sup_{\xi > 1} f(\xi) = f(\xi_0) = 4 \log(\sqrt{2} - 1),$$

so that

$$\tilde{R}_n(t) \cdot \frac{t^4 (t+n)}{(2t+n)(t+2n)} < e^2 (n+1)^2 (\sqrt{2}-1)^{4n}. \tag{12}$$

Finally,

$$\tilde{F}_n = \sum_{t=n+1}^{\infty} \tilde{R}_n(t) < e^2(n+1)^2 (\sqrt{2}-1)^{4n} \sum_{t=n+1}^{\infty} \frac{(2t+n)(t+2n)}{t^4(t+n)}$$

$$< e^2(n+1)^2 (\sqrt{2}-1)^{4n} \big( 2\zeta(5) + 5n\zeta(4) + 2n^2 \zeta(3) \big) < 20(n+1)^4 (\sqrt{2}-1)^{4n}.$$

This completes the proof. $\square$

## Lemma 7 (Coincidence of the Two Series)

**Statement.** For each $n = 0, 1, 2, \ldots$, the quantities $F_n$ from (1) and $\tilde{F}_n$ from (9) coincide.

**Proof.** Both $F_n$ and $\tilde{F}_n$ satisfy the same second-order difference equation

$$(n+1)^3 X_{n+1} - (2n+1)(17n^2 + 17n + 5) X_n + n^3 X_{n-1} = 0, \tag{7}$$

so we have to verify that $F_0 = \tilde{F}_0$ and $F_1 = \tilde{F}_1$. Direct calculations show that

$$\tilde{R}_0(t) = \frac{2}{t^3}, \qquad \tilde{R}_1(t) = -\frac{2}{t^4} + \frac{2}{(t+1)^4} + \frac{5}{t^3} + \frac{5}{(t+1)^3} - \frac{5}{t^2} + \frac{5}{(t+1)^2},$$

hence $\tilde{F}_0 = 2\zeta(3)$ and $\tilde{F}_1 = 10\zeta(3) - 12$, and comparison with (4) yields the desired coincidence. $\square$

## Apéry's Theorem

**Statement.** The number $\zeta(3)$ is irrational.

**Proof.** Suppose, on the contrary, that $\zeta(3) = p/q$, where $p$ and $q$ are positive integers. Then, using the trivial bound $D_n < 3^n$, we deduce that, for each $n = 0, 1, 2, \ldots$, the integer

$$q D_n^3 F_n = D_n^3 u_n p - D_n^3 v_n q$$

satisfies the estimate

$$0 < q D_n^3 F_n < 20 q (n+1)^4 \cdot 3^{3n} (\sqrt{2} - 1)^{4n}, \tag{15}$$

which is not possible since

$$3^3 (\sqrt{2} - 1)^4 = 0.7948\ldots < 1,$$

so the right-hand side of (15) is less than $1$ for sufficiently large integer $n$. This contradiction completes the proof of the theorem. $\square$

## Remark

In spite of its elementary arguments, this proof of Apéry's theorem does not look simpler than the original (also elementary) Apéry's proof well-explained in A. van der Poorten's informal report, or the (almost elementary) Beukers's proof by means of Legendre polynomials and multiple integrals. The way to deduce the recursion (7) for the sequence $F_n$ and for the coefficients $u_n, v_n$ slightly differs from previous approaches although it is based on the same algorithm of creative telescoping.

The fact that $F_n = u_n \zeta(3) - v_n$ with $D_n u_n, D_n^4 v_n \in \mathbb{Z}$ was first discovered by K. Ball; the proof follows lines of the proof of Lemma 1. An open question of T. Rivoal here is to get the better inclusions $u_n, D_n^3 v_n \in \mathbb{Z}$ by elementary means without going back to Apéry's series (1). A solution of this question, accompanied with Ball's Lemma 4, can bring the "most elementary" proof of Apéry's theorem.

## Key References

- R. Apéry, *Irrationalité de $\zeta(2)$ et $\zeta(3)$*, Astérisque **61** (1979), 11–13.
- K. Ball and T. Rivoal, *Irrationalité d'une infinité de valeurs de la fonction zêta aux entiers impairs*, Invent. Math. **146** (2001), 193–207.
- F. Beukers, *A note on the irrationality of $\zeta(2)$ and $\zeta(3)$*, Bull. London Math. Soc. **11** (1979), 268–272.
- Yu. V. Nesterenko, *A few remarks on $\zeta(3)$*, Mat. Zametki **59** (1996), 865–880.
- M. Petkovšek, H. S. Wilf, and D. Zeilberger, *$A = B$*, A. K. Peters, 1997.
- A. van der Poorten, *A proof that Euler missed... Apéry's proof of the irrationality of $\zeta(3)$*, Math. Intelligencer **1** (1978/79), 195–203.
