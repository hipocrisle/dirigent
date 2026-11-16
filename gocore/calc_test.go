package gocore

import "testing"

func TestDiv(t *testing.T) {
	tests := []struct {
		name    string
		a, b    int
		want    int
		wantErr bool
	}{
		{name: "обычное деление", a: 10, b: 2, want: 5},
		{name: "деление с остатком", a: 7, b: 2, want: 3},
		{name: "ноль делимое", a: 0, b: 5, want: 0},
		{name: "деление на ноль", a: 10, b: 0, wantErr: true},
		{name: "ноль на ноль", a: 0, b: 0, wantErr: true},
		{name: "оба отрицательные", a: -10, b: -2, want: 5},
		{name: "отрицательное делимое", a: -9, b: 3, want: -3},
		{name: "отрицательный делитель", a: 9, b: -3, want: -3},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := Div(tt.a, tt.b)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("Div(%d, %d): ожидалась ошибка, получено %d, nil", tt.a, tt.b, got)
				}
				if err.Error() != "division by zero" {
					t.Fatalf("Div(%d, %d): неверный текст ошибки: %q", tt.a, tt.b, err.Error())
				}
				return
			}
			if err != nil {
				t.Fatalf("Div(%d, %d): неожиданная ошибка: %v", tt.a, tt.b, err)
			}
			if got != tt.want {
				t.Fatalf("Div(%d, %d) = %d, ожидалось %d", tt.a, tt.b, got, tt.want)
			}
		})
	}
}

func TestMod(t *testing.T) {
	tests := []struct {
		name    string
		a, b    int
		want    int
		wantErr bool
	}{
		{name: "без остатка", a: 10, b: 2, want: 0},
		{name: "с остатком", a: 7, b: 2, want: 1},
		{name: "делимое меньше делителя", a: 3, b: 5, want: 3},
		{name: "ноль делимое", a: 0, b: 5, want: 0},
		{name: "остаток от деления на ноль", a: 10, b: 0, wantErr: true},
		{name: "ноль на ноль", a: 0, b: 0, wantErr: true},
		{name: "отрицательное делимое", a: -7, b: 2, want: -1},
		{name: "оба отрицательные", a: -7, b: -2, want: -1},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := Mod(tt.a, tt.b)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("Mod(%d, %d): ожидалась ошибка, получено %d, nil", tt.a, tt.b, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("Mod(%d, %d): неожиданная ошибка: %v", tt.a, tt.b, err)
			}
			if got != tt.want {
				t.Fatalf("Mod(%d, %d) = %d, ожидалось %d", tt.a, tt.b, got, tt.want)
			}
		})
	}
}
