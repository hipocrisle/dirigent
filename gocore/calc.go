package gocore

import "errors"

// Div возвращает частное a/b. При b == 0 возвращает ошибку.
func Div(a, b int) (int, error) {
	if b == 0 {
		return 0, errors.New("division by zero")
	}
	return a / b, nil
}

// Mod возвращает остаток a % b. При b == 0 возвращает ошибку.
func Mod(a, b int) (int, error) {
	if b == 0 {
		return 0, errors.New("остаток от деления на ноль")
	}
	return a % b, nil
}
